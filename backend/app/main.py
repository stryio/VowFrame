"""Local FastAPI entry point and application factory."""

import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import router
from .api88 import Api88Generator
from .config import Config, load_config
from .db import Database
from .director import Api88Director, MockDirector
from .generator import ImageGenerator, MockGenerator
from .face_swap import FaceSwapper, create_face_swapper
from .photo_prep import FacePhotoPreparer, PhotoPreparer
from .task_manager import TaskManager

logger = logging.getLogger(__name__)
FRONTEND_DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'


def _configure_file_logging(data_dir: Path):
    log_dir = data_dir / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / 'vowframe.log').resolve()
    app_logger = logging.getLogger('app')
    if app_logger.level == logging.NOTSET or app_logger.level > logging.INFO:
        app_logger.setLevel(logging.INFO)
    for handler in list(app_logger.handlers):
        if getattr(handler, '_vowframe_file_handler', False):
            if Path(handler.baseFilename).resolve() == log_path:
                return
            app_logger.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    handler._vowframe_file_handler = True
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z',
    ))
    app_logger.addHandler(handler)


def create_app(config: Config | None = None, generator: ImageGenerator | None = None,
               face_swapper: FaceSwapper | None = None, photo_preparer: PhotoPreparer | None = None,
               stylist: ImageGenerator | None = None) -> FastAPI:
    config = config if config is not None else load_config()
    if generator is None:
        if config.generator == 'mock':
            generator = MockGenerator(config.mock_delay_seconds)
        elif config.generator == '88api':
            generator = Api88Generator(config)
        else:
            raise ValueError('不支持此生成器，请配置 mock 或 88api')
    director = MockDirector() if config.generator == 'mock' else Api88Director(config)
    face_swapper = face_swapper or create_face_swapper(config)
    db = Database(config.db_path)
    manager = TaskManager(config, db, generator, face_swapper, stylist)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        _configure_file_logging(config.data_dir)
        # config.local.json's aspect_ratio only seeds the first-run portrait ratio; Settings owns it afterwards.
        db.initialize(config.model, config.director_model, config.aspect_ratio)
        if hasattr(director, 'model'):
            director.model = db.get_director_model()
        manager.recover()
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    application = FastAPI(title='VowFrame', lifespan=lifespan)
    application.state.config = config
    application.state.db = db
    application.state.task_manager = manager
    application.state.generator = generator
    application.state.face_swapper = face_swapper
    application.state.director = director
    application.state.photo_preparer = photo_preparer or FacePhotoPreparer()

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if not isinstance(detail, dict) or 'code' not in detail or 'message' not in detail:
            messages = {404: '接口或资源不存在', 405: '该接口不支持此请求方法'}
            detail = {'code': 'not_found' if exc.status_code == 404 else 'http_error', 'message': messages.get(exc.status_code, '请求处理失败')}
        return JSONResponse(status_code=exc.status_code, content={'detail': detail}, headers=exc.headers)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={'detail': {'code': 'validation_error', 'message': '请求参数无效，请检查输入内容'}})

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error('Unhandled API error', exc_info=exc)
        return JSONResponse(status_code=500, content={'detail': {'code': 'internal_error', 'message': '服务处理失败，请稍后重试'}})

    application.include_router(router)

    # The catch-all is registered last and explicitly excludes the API namespace.
    @application.get('/{path:path}', include_in_schema=False)
    async def frontend(path: str):
        if path == 'api' or path.startswith('api/'):
            raise StarletteHTTPException(404)
        root = FRONTEND_DIST.resolve()
        requested = (root / path).resolve()
        if not requested.is_relative_to(root):
            raise StarletteHTTPException(404)
        if requested.is_file():
            return FileResponse(requested)
        index = root / 'index.html'
        if index.is_file() and (not Path(path).suffix or path == 'index.html'):
            return FileResponse(index)
        raise StarletteHTTPException(404)

    return application


app = create_app()

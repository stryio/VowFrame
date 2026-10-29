"""FaceFusion subprocess entry point. It is run with FaceFusion's own venv."""

import contextlib
import json
import sys
from pathlib import Path


def _emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def _largest_face(faces, order):
    ordered = order(faces, 'large-small')
    return ordered[0] if ordered else None


def _face_turn(face):
    """Nose offset from the eye midpoint along the eye line, in eye distances (independent of head tilt).

    Negative: the face turns toward the picture's left; positive: toward the picture's right.
    """
    (lx, ly), (rx, ry), (nx, ny) = ((float(x), float(y)) for x, y in face.landmark_set['5/68'][:3])
    ax, ay = rx - lx, ry - ly
    return ((nx - (lx + rx) / 2) * ax + (ny - (ly + ry) / 2) * ay) / (ax * ax + ay * ay)


def _facing(turn, side_turn):
    if turn <= -side_turn:
        return 'left'
    if turn >= side_turn:
        return 'right'
    return 'front'


def _cuda_face_swapper_used(tracked_sessions, expected_models):
    """True when every face-swap model that ran executed on CUDA."""
    expected_models = {f'{model}.onnx' for model in expected_models}
    cuda_models = set()
    for model_name, session in tracked_sessions:
        profile_path = session.end_profiling()
        if model_name not in expected_models:
            continue
        try:
            with open(profile_path, encoding='utf-8') as profile_file:
                events = json.load(profile_file)
        except (OSError, ValueError):
            continue
        if any(
            isinstance(event, dict)
            and event.get('args', {}).get('provider') == 'CUDAExecutionProvider'
            for event in events
        ):
            cuda_models.add(model_name)
    return cuda_models == expected_models


def _run(payload):
    facefusion_dir = Path(payload['facefusion_dir']).resolve()
    sys.path.insert(0, str(facefusion_dir))

    import onnxruntime
    import facefusion.inference_manager as inference_manager
    from facefusion import core, logger, state_manager
    from facefusion.face_creator import get_static_faces
    from facefusion.face_selector import sort_faces_by_order
    from facefusion.jobs import job_manager
    from facefusion.program import create_program
    from facefusion.vision import read_static_image
    from facefusion.args import apply_args

    if 'CUDAExecutionProvider' not in onnxruntime.get_available_providers():
        raise RuntimeError('CUDAExecutionProvider is not available in this FaceFusion environment')

    work_dir = Path(payload['work_dir']).resolve()
    jobs_path = str(work_dir / 'jobs')
    profiles_dir = work_dir / 'profiles'
    profiles_dir.mkdir(parents=True, exist_ok=True)
    tracked_sessions = []

    def create_profiled_session(model_path, providers):
        options = onnxruntime.SessionOptions()
        options.enable_profiling = True
        options.profile_file_prefix = str(profiles_dir / f'model-{len(tracked_sessions)}')
        session = onnxruntime.InferenceSession(
            model_path,
            sess_options=options,
            providers=providers,
        )
        tracked_sessions.append((Path(model_path).name, session))
        return session

    inference_manager.create_inference_session = create_profiled_session

    temp_path = str(work_dir / 'facefusion-temp')
    base_args = [
        'headless-run',
        '--config-path', str(facefusion_dir / 'facefusion.ini'),
        '--jobs-path', jobs_path,
        '--temp-path', temp_path,
        '--execution-providers', 'cuda',
        '--processors', 'face_swapper',
        '--face-swapper-model', payload['front_model'],
        '--face-swapper-pixel-boost', '512x512',
        '--face-swapper-weight', str(payload['weight']),
        # The occlusion mask keeps hair strands, ears, veils and hair ornaments in front of the face untouched.
        '--face-mask-types', 'box', 'occlusion',
        '--face-selector-mode', 'many',
        '--face-selector-order', 'large-small',
        '--face-detector-model', 'yolo_face',
        # Keep provider sessions loaded between the two image passes.
        '--video-memory-strategy', 'tolerant',
    ]
    target_suffix = Path(payload['target_path']).suffix or '.png'
    bride_output_path = work_dir / f'bride-pass{target_suffix}'
    raw_output_path = work_dir / f'final-pass{target_suffix}'
    sys.argv = [str(facefusion_dir / 'facefusion.py'), *base_args]
    parser = create_program()
    state_args = vars(parser.parse_args(base_args + [
        '--face-selector-gender', 'female',
        '-s', payload['bride']['front'],
        '-t', payload['target_path'],
        '-o', str(bride_output_path),
    ]))
    with contextlib.redirect_stdout(sys.stderr):
        logger.init('error')
        apply_args(state_args, state_manager.init_item)
        state_manager.set_item('face_detector_model', 'yolo_face')
        if not core.common_pre_check():
            raise RuntimeError('FaceFusion content-analyser source validation failed')
        if not job_manager.init_jobs(jobs_path):
            raise RuntimeError('FaceFusion could not initialize its temporary job directory')

        def largest_face(path):
            frame = read_static_image(path)
            if frame is None:
                raise RuntimeError('A face-swap input image could not be decoded')
            return _largest_face(get_static_faces([frame]), sort_faces_by_order)

        people = {'bride': 'female', 'groom': 'male'}
        for role, gender in people.items():
            face = largest_face(payload[role]['front'])
            if face is None or face.gender != gender:
                return {'ok': False, 'error_code': 'roles_swapped'}

        target_frame = read_static_image(payload['target_path'])
        if target_frame is None:
            raise RuntimeError('A face-swap input image could not be decoded')
        target_faces = get_static_faces([target_frame])
        targets = {role: _largest_face([face for face in target_faces if face.gender == gender], sort_faces_by_order)
                   for role, gender in people.items()}
        missing = [role for role, face in targets.items() if face is None]
        if missing:
            return {'ok': False, 'error_code': 'face_not_found', 'missing': missing}

        # Each person's model and sources follow how their (largest) face turns in the generated image.
        faces = {}
        plans = {}
        for role, gender in people.items():
            turn = _face_turn(targets[role])
            facing = _facing(turn, payload['side_turn'])
            sources = [payload[role]['front']]
            face = {'turn': round(turn, 3), 'facing': facing,
                    'model': payload['front_model'] if facing == 'front' else payload['side_model']}
            side_path = payload[role].get(facing) if facing != 'front' else None
            if side_path:
                side_face = largest_face(side_path)
                if side_face is not None and side_face.gender == gender:
                    sources.append(side_path)
                else:
                    # The optional side photo cannot be used; record it rather than hide it.
                    face['side_photo_unusable'] = True
            face['sources'] = len(sources)
            faces[role] = face
            plans[role] = sources

        pass_args = dict(state_args)
        pass_args.update({
            'execution_providers': ['cuda'],
            'processors': ['face_swapper'],
            'face_swapper_pixel_boost': '512x512',
            'face_swapper_weight': payload['weight'],
            'face_mask_types': ['box', 'occlusion'],
            'face_selector_mode': 'many',
        })
        passes = (('bride', payload['target_path'], bride_output_path),
                  ('groom', str(bride_output_path), raw_output_path))
        for role, target_path, output_path in passes:
            pass_args.update({
                'face_selector_gender': people[role],
                # Several source photos: FaceFusion averages their identities.
                'source_paths': plans[role],
                'target_path': target_path,
                'output_path': str(output_path),
                'face_swapper_model': faces[role]['model'],
            })
            apply_args(pass_args, state_manager.set_item)
            if core.process_headless(pass_args) != 0:
                raise RuntimeError(f'FaceFusion {role} pass failed')

        swapper_cuda_used = _cuda_face_swapper_used(tracked_sessions, {face['model'] for face in faces.values()})

        if not raw_output_path.is_file():
            raise RuntimeError('FaceFusion did not create the output image')
        final_frame = read_static_image(str(raw_output_path))
        if final_frame is None:
            raise RuntimeError('FaceFusion output image could not be decoded')
        from facefusion.vision import write_image
        if not write_image(payload['output_path'], final_frame):
            raise RuntimeError('FaceFusion could not encode the PNG output')
        return {'ok': True, 'cuda_face_swapper_used': swapper_cuda_used, 'faces': faces}


def main():
    try:
        payload = json.load(sys.stdin)
        result = _run(payload)
    except Exception as exc:
        result = {'ok': False, 'error_code': 'face_swap_failed', 'diagnostic': type(exc).__name__}
    _emit(result)


if __name__ == '__main__':
    main()

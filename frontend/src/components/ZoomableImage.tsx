import { useState } from "react";
import { useImageViewport, type ImagePoint } from "./useImageViewport";

export function ImageZoomControls({ viewport, disabled = false }: {
  viewport: ReturnType<typeof useImageViewport>;
  disabled?: boolean;
}) {
  return (
    <div className="image-zoom-controls" role="group" aria-label="图片缩放">
      <button type="button" className="btn btn-sm" aria-label="缩小图片" disabled={disabled || !viewport.canZoomOut} onClick={viewport.zoomOut}>−</button>
      <span className="image-zoom-level" aria-live="polite" title="相对于适应窗口的大小">{Math.round(viewport.view.scale * 100)}%</span>
      <button type="button" className="btn btn-sm" aria-label="放大图片" disabled={disabled || !viewport.canZoomIn} onClick={viewport.zoomIn}>＋</button>
      <button type="button" className="btn btn-sm" disabled={disabled} onClick={viewport.reset}>适应窗口</button>
    </div>
  );
}

/** Both regular viewing entry points use the same navigation behavior. */
export function ZoomableImage({ src, alt }: { src: string; alt: string }) {
  return <ImageViewport key={src} src={src} alt={alt} />;
}

function ImageViewport({ src, alt }: { src: string; alt: string }) {
  const [size, setSize] = useState<ImagePoint | null>(null);
  const [error, setError] = useState(false);
  const viewport = useImageViewport(size, { pan: true });
  return (
    <div className="zoomable-image">
      <div ref={viewport.stage} className={`image-view-stage ${viewport.canZoomOut ? "is-zoomed" : ""} ${viewport.navigating ? "is-panning" : ""}`}
        aria-label="图片查看区域，可滚轮缩放，放大后拖动" {...viewport.handlers}
        onDoubleClick={(event) => viewport.toggleZoom(event.clientX, event.clientY)}>
        <img src={src} alt={alt} draggable={false} style={{ width: viewport.frame.x, height: viewport.frame.y, transform: viewport.transform, visibility: size ? "visible" : "hidden" }}
          onLoad={(event) => setSize({ x: event.currentTarget.naturalWidth, y: event.currentTarget.naturalHeight })}
          onError={() => setError(true)} />
        {!size && <span className="image-view-loading">{error ? "图片加载失败，请重新打开" : "正在加载图片…"}</span>}
      </div>
      <ImageZoomControls viewport={viewport} disabled={!size} />
      <span className="image-zoom-hint">滚轮或双指缩放 · 放大后拖动 · 双击放大或还原</span>
    </div>
  );
}

import { useEffect, useRef, useState, type PointerEvent } from "react";

export type ImagePoint = { x: number; y: number };
type View = ImagePoint & { scale: number };
const MAX_SCALE = 8;
const INITIAL_VIEW: View = { scale: 1, x: 0, y: 0 };

/** Keep navigation in display pixels; annotations remain in original-image coordinates. */
export function useImageViewport(size: ImagePoint | null, options: {
  pan?: boolean;
  disabled?: boolean;
  onNavigate?: () => void;
} = {}) {
  const stage = useRef<HTMLDivElement>(null);
  const [bounds, setBounds] = useState<ImagePoint>({ x: 1, y: 1 });
  const [view, setView] = useState<View>(INITIAL_VIEW);
  const current = useRef(view);
  const config = useRef(options);
  config.current = options;
  const pointers = useRef(new Map<number, ImagePoint>());
  const gesture = useRef<"pan" | "pinch" | "blocked" | null>(null);
  const [navigating, setNavigating] = useState(false);
  const fit = size ? Math.min(bounds.x / size.x, bounds.y / size.y) : 1;
  const frame = size ? { x: size.x * fit, y: size.y * fit } : bounds;

  const constrain = (next: View): View => {
    const scale = Math.max(1, Math.min(MAX_SCALE, next.scale));
    const maxX = Math.max(0, (frame.x * scale - bounds.x) / 2);
    const maxY = Math.max(0, (frame.y * scale - bounds.y) / 2);
    return { scale, x: Math.max(-maxX, Math.min(maxX, next.x)), y: Math.max(-maxY, Math.min(maxY, next.y)) };
  };
  const apply = (next: View) => {
    current.current = constrain(next);
    setView(current.current);
  };
  const beginNavigation = () => {
    config.current.onNavigate?.();
    setNavigating(true);
  };
  const zoomAt = (scale: number, anchor: ImagePoint = { x: 0, y: 0 }) => {
    if (config.current.disabled || !size) return;
    config.current.onNavigate?.();
    const previous = current.current;
    const ratio = Math.max(1, Math.min(MAX_SCALE, scale)) / previous.scale;
    apply({ scale: previous.scale * ratio,
      x: anchor.x - (anchor.x - previous.x) * ratio,
      y: anchor.y - (anchor.y - previous.y) * ratio });
  };
  const local = (clientX: number, clientY: number): ImagePoint => {
    const rect = stage.current!.getBoundingClientRect();
    return { x: clientX - rect.left - rect.width / 2, y: clientY - rect.top - rect.height / 2 };
  };

  useEffect(() => {
    const element = stage.current;
    if (!element) return;
    const resize = () => setBounds({ x: element.clientWidth, y: element.clientHeight });
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    apply(current.current);
    // Re-clamp translation after the available viewport or image dimensions change.
  }, [bounds.x, bounds.y, size?.x, size?.y]);

  // The native listener must be non-passive to prevent scrolling the surrounding dialog.
  const wheelHandler = useRef<(event: WheelEvent) => void>(() => {});
  wheelHandler.current = (event) => {
    if (config.current.disabled || !size) return;
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.y : 1);
    zoomAt(current.current.scale * Math.exp(-Math.max(-100, Math.min(100, delta)) * 0.002), local(event.clientX, event.clientY));
  };
  useEffect(() => {
    const element = stage.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => wheelHandler.current(event);
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, []);

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (config.current.disabled || !size || event.button !== 0) return;
    pointers.current.set(event.pointerId, local(event.clientX, event.clientY));
    if (pointers.current.size >= 2) {
      gesture.current = "pinch";
      beginNavigation();
    } else if (config.current.pan && current.current.scale > 1) {
      gesture.current = "pan";
      beginNavigation();
    }
    if (gesture.current) {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const old = pointers.current.get(event.pointerId);
    if (!old || config.current.disabled) return;
    const next = local(event.clientX, event.clientY);
    const other = [...pointers.current.entries()].find(([id]) => id !== event.pointerId)?.[1];
    pointers.current.set(event.pointerId, next);
    if (gesture.current === "pinch" && other) {
      const distance = Math.hypot(old.x - other.x, old.y - other.y);
      if (distance < 1) return;
      const previous = current.current;
      const scale = Math.max(1, Math.min(MAX_SCALE, previous.scale * Math.hypot(next.x - other.x, next.y - other.y) / distance));
      const ratio = scale / previous.scale;
      apply({ scale,
        x: (next.x + other.x) / 2 - ((old.x + other.x) / 2 - previous.x) * ratio,
        y: (next.y + other.y) / 2 - ((old.y + other.y) / 2 - previous.y) * ratio });
    } else if (gesture.current === "pan") {
      apply({ ...current.current, x: current.current.x + next.x - old.x, y: current.current.y + next.y - old.y });
    }
  };
  const onPointerEnd = (event: PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
    if (!pointers.current.size) {
      gesture.current = null;
      setNavigating(false);
    } else if (gesture.current) {
      // A remaining finger after a pinch must not start painting or jump the image.
      gesture.current = "blocked";
    }
  };

  return {
    stage, frame, view, navigating,
    transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
    zoomIn: () => zoomAt(current.current.scale * 1.5),
    zoomOut: () => zoomAt(current.current.scale / 1.5),
    reset: () => { if (!config.current.disabled) { config.current.onNavigate?.(); apply(INITIAL_VIEW); } },
    toggleZoom: (clientX: number, clientY: number) => zoomAt(current.current.scale > 1 ? 1 : 2.5, local(clientX, clientY)),
    canZoomIn: view.scale < MAX_SCALE,
    canZoomOut: view.scale > 1,
    isGestureActive: () => gesture.current !== null,
    handlers: { onPointerDown, onPointerMove, onPointerUp: onPointerEnd, onPointerCancel: onPointerEnd,
      onLostPointerCapture: onPointerEnd },
  };
}

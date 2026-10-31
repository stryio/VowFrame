import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { assetUrl, type FaceScores } from "../api";
import { FaceScore } from "./FaceScore";
import { useCurrentProject } from "./Layout";

export interface CompareAction {
  label: string;
  onSelect: () => void;
  primary?: boolean;
  disabled?: boolean;
}

export interface CompareItem {
  key: string;
  assetId: string;
  title: string;
  subtitle?: string;
  scores?: FaceScores | null;
  actions?: CompareAction[];
}

type Mode = "flip" | "side";

const MIN_SCALE = 1;
const MAX_SCALE = 8;

/**
 * Full-screen comparison (PRD 4.6). Always opens side by side, each image with its face
 * similarity; "flip" (one image in place, hold Space to see the other, shared zoom) stays
 * available for two images of the same composition. The uploaded photos stay visible,
 * because "像不像" is judged against them, not against another generated image.
 */
export function CompareView({
  title,
  items,
  aligned,
  onClose,
}: {
  title: string;
  items: CompareItem[];
  /** True when the items share one composition, so flipping in place makes sense. */
  aligned: boolean;
  onClose: () => void;
}) {
  const canFlip = items.length === 2;
  const [mode, setMode] = useState<Mode>("side");
  const [base, setBase] = useState(0);
  const [peek, setPeek] = useState(false);
  const [showRefs, setShowRefs] = useState(true);
  const references = useReferences();

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.code === "Space" && mode === "flip") {
        e.preventDefault();
        if (!e.repeat) setPeek(true);
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === "Space") setPeek(false);
    };
    // Releasing Space outside the window must not leave the other image stuck on screen.
    const blur = () => setPeek(false);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  }, [mode, onClose]);

  const shown = peek ? 1 - base : base;

  return (
    <div className="compare">
      <header className="compare-header">
        <h2>{title}</h2>
        <div className="compare-modes" role="group" aria-label="对比方式">
          <button
            className={`shot-pill ${mode === "flip" ? "active" : ""}`}
            onClick={() => setMode("flip")}
            disabled={!canFlip}
            title={canFlip ? "同一位置切换，适合只有脸不同的两张" : "切换只能对比两张"}
          >
            切换
          </button>
          <button className={`shot-pill ${mode === "side" ? "active" : ""}`} onClick={() => setMode("side")}>
            并排
          </button>
        </div>
        <span className="selection-spacer" />
        <button className="btn btn-sm compare-ghost" onClick={() => setShowRefs((v) => !v)}>
          {showRefs ? "收起原照" : "显示原照"}
        </button>
        <button className="btn btn-sm compare-ghost" onClick={onClose}>
          关闭
        </button>
      </header>

      <div className="compare-body">
        {mode === "flip" && canFlip ? (
          <div className="compare-flip">
            <ZoomPane layers={items.map((i) => i.assetId)} shown={shown} />
            <div className="compare-shown">
              正在显示：<strong>{items[shown].title}</strong>
              {!aligned && <span className="compare-warn">两张构图不同，切换对比可能不准确</span>}
            </div>
            <div className="compare-flip-bar">
              {items.map((item, index) => (
                <div key={item.key} className={`compare-chip ${shown === index ? "active" : ""}`}>
                  <button className="compare-chip-label" onClick={() => setBase(index)} title="设为默认显示">
                    <span>{item.title}</span>
                    {item.subtitle && <span className="compare-sub">{item.subtitle}</span>}
                    <FaceScore scores={item.scores} />
                  </button>
                  <Actions actions={item.actions} />
                </div>
              ))}
              <button
                className="btn btn-gold compare-hold"
                onPointerDown={() => setPeek(true)}
                onPointerUp={() => setPeek(false)}
                onPointerLeave={() => setPeek(false)}
              >
                按住看另一张（或按住空格）
              </button>
            </div>
          </div>
        ) : (
          <div className="compare-side" style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}>
            {items.map((item) => (
              <div key={item.key} className="compare-cell">
                <div className="compare-cell-title">
                  <span>{item.title}</span>
                  {item.subtitle && <span className="compare-sub">{item.subtitle}</span>}
                  <FaceScore scores={item.scores} />
                </div>
                <ZoomPane layers={[item.assetId]} shown={0} />
                <Actions actions={item.actions} />
              </div>
            ))}
          </div>
        )}

        {showRefs && references.length > 0 && (
          <aside className="compare-refs">
            <span className="compare-sub">原照</span>
            {references.map((ref) => (
              <figure key={ref.assetId}>
                <img src={assetUrl(ref.assetId)} alt={ref.label} />
                <figcaption>{ref.label}</figcaption>
              </figure>
            ))}
          </aside>
        )}
      </div>
      <footer className="compare-hint">滚轮放大 · 放大后拖动移动 · 双击放大或还原 · Esc 关闭</footer>
    </div>
  );
}

function Actions({ actions }: { actions?: CompareAction[] }) {
  if (!actions?.length) return null;
  return (
    <div className="compare-actions">
      {actions.map((a) => (
        <button
          key={a.label}
          className={`btn btn-sm ${a.primary ? "btn-gold" : "compare-ghost"}`}
          onClick={a.onSelect}
          disabled={a.disabled}
        >
          {a.label}
        </button>
      ))}
    </div>
  );
}

/** The project's current uploaded photos, bride first. */
function useReferences() {
  const { project } = useCurrentProject();
  if (!project) return [];
  const people = [project.person_a, project.person_b];
  return (["bride", "groom"] as const).flatMap((role) => {
    const assetId = people.find((p) => p.role === role)?.asset_id;
    return assetId ? [{ label: role === "bride" ? "新娘" : "新郎", assetId }] : [];
  });
}

/**
 * Zoomable image area. All layers share one transform, so flipping between them keeps
 * the exact same region on screen. Hidden layers stay mounted to avoid a decode flash.
 */
function ZoomPane({ layers, shown }: { layers: string[]; shown: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ s: 1, x: 0, y: 0 });
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(null);

  // Keep the content covering the pane: never pan past its edges.
  const clamp = (s: number, x: number, y: number) => {
    const el = ref.current;
    if (!el || s <= MIN_SCALE) return { s: MIN_SCALE, x: 0, y: 0 };
    const { width, height } = el.getBoundingClientRect();
    return {
      s,
      x: Math.min(0, Math.max(width - width * s, x)),
      y: Math.min(0, Math.max(height - height * s, y)),
    };
  };

  const zoomAt = (clientX: number, clientY: number, next: (s: number) => number) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    setView((v) => {
      const s = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next(v.s)));
      return clamp(s, px - ((px - v.x) * s) / v.s, py - ((py - v.y) * s) / v.s);
    });
  };

  // React's onWheel is passive, so preventDefault (stop page scroll) needs a native listener.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, (s) => s * (e.deltaY < 0 ? 1.2 : 1 / 1.2));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onPointerDown = (e: ReactPointerEvent) => {
    if (view.s <= MIN_SCALE) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { px: e.clientX, py: e.clientY, x: view.x, y: view.y };
  };

  const onPointerMove = (e: ReactPointerEvent) => {
    const start = drag.current;
    if (!start) return;
    setView((v) => clamp(v.s, start.x + e.clientX - start.px, start.y + e.clientY - start.py));
  };

  return (
    <div
      ref={ref}
      className={`zoom-pane ${view.s > MIN_SCALE ? "zoomed" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={() => (drag.current = null)}
      onPointerCancel={() => (drag.current = null)}
      onDoubleClick={(e) => zoomAt(e.clientX, e.clientY, (s) => (s > MIN_SCALE ? MIN_SCALE : 2.5))}
    >
      <div className="zoom-content" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.s})` }}>
        {layers.map((assetId, index) => (
          <img
            key={assetId}
            src={assetUrl(assetId)}
            alt=""
            draggable={false}
            style={{ opacity: index === shown ? 1 : 0 }}
          />
        ))}
      </div>
    </div>
  );
}

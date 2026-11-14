import { useEffect, useRef, useState, type PointerEvent } from "react";
import { api, ApiError, assetUrl, TASK_STATUS_LABEL, type HandAnalysis, type ImageProvider, type ImageProviderId, type Model, type Slot } from "../api";
import { usePolledResource } from "../hooks";
import { useToast } from "./feedback";
import { ImageZoomControls } from "./ZoomableImage";
import { useImageViewport } from "./useImageViewport";

type Point = { x: number; y: number };
type Tool = "ellipse" | "brush" | "rectangle";
type Kind = "crop" | "reference" | "markup" | "mask";
// Marks that cover the area to change (filled), rather than a ring around it.
const filled = (kind: Kind) => kind === "mask" || kind === "crop";
type Stroke = { tool: Tool; points: Point[]; width: number };
type CircleMove = { index: number; start: Point; original: Stroke; stroke: Stroke };

function circleAt(strokes: Stroke[], point: Point, tolerance: Point): number {
  // Prefer the last drawn circle when multiple circles overlap.
  for (let index = strokes.length - 1; index >= 0; index--) {
    const stroke = strokes[index];
    if (stroke.tool !== "ellipse" && stroke.tool !== "rectangle") continue;
    const first = stroke.points[0], last = stroke.points[stroke.points.length - 1];
    const radiusX = Math.abs(last.x - first.x) / 2 + tolerance.x;
    const radiusY = Math.abs(last.y - first.y) / 2 + tolerance.y;
    if (stroke.tool === "rectangle") {
      if (Math.abs(point.x - (first.x + last.x) / 2) <= radiusX
        && Math.abs(point.y - (first.y + last.y) / 2) <= radiusY) return index;
      continue;
    }
    if (((point.x - (first.x + last.x) / 2) / radiusX) ** 2
      + ((point.y - (first.y + last.y) / 2) / radiusY) ** 2 <= 1) return index;
  }
  return -1;
}

function translateCircle(move: CircleMove, point: Point): Stroke {
  const xs = move.original.points.map((p) => p.x), ys = move.original.points.map((p) => p.y);
  const dx = Math.max(-Math.min(...xs), Math.min(1 - Math.max(...xs), point.x - move.start.x));
  const dy = Math.max(-Math.min(...ys), Math.min(1 - Math.max(...ys), point.y - move.start.y));
  return { ...move.original, points: move.original.points.map((p) => ({ x: p.x + dx, y: p.y + dy })) };
}

const HAND_INSTRUCTION = "只修复选中区域内畸形、粘连或多余的手指，使手部结构自然。保持原有手势、牵手或握物关系，以及戒指、肤色和光线；被衣袖、捧花或另一只手遮挡的部分保持遮挡，不强行补出所有手指。";

function drawStroke(ctx: CanvasRenderingContext2D, stroke: Stroke, fill: boolean) {
  const points = stroke.points.map((p) => ({ x: p.x * ctx.canvas.width, y: p.y * ctx.canvas.height }));
  const first = points[0];
  if (!first) return;
  ctx.lineWidth = stroke.width * Math.min(ctx.canvas.width, ctx.canvas.height);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  if (stroke.tool === "rectangle") {
    const last = points[points.length - 1];
    ctx.rect(first.x, first.y, last.x - first.x, last.y - first.y);
    if (fill) ctx.fill();
    else ctx.stroke();
  } else if (stroke.tool === "ellipse") {
    const last = points[points.length - 1];
    ctx.ellipse((first.x + last.x) / 2, (first.y + last.y) / 2,
      Math.max(0.5, Math.abs(first.x - last.x) / 2), Math.max(0.5, Math.abs(first.y - last.y) / 2), 0, 0, Math.PI * 2);
    if (fill) ctx.fill();
    else ctx.stroke();
  } else if (points.length === 1) {
    ctx.arc(first.x, first.y, ctx.lineWidth / 2, 0, Math.PI * 2);
    ctx.fill();
  } else {
    ctx.moveTo(first.x, first.y);
    points.slice(1).forEach((p) => ctx.lineTo(p.x, p.y));
    ctx.stroke();
  }
}

function annotationBlob(size: Point, strokes: Stroke[], kind: Kind): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = size.x;
  canvas.height = size.y;
  const ctx = canvas.getContext("2d");
  if (!ctx) return Promise.reject(new Error("浏览器无法创建标记图，请更换浏览器后重试"));
  if (kind === "mask") {
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.globalCompositeOperation = "destination-out";
  }
  ctx.strokeStyle = ctx.fillStyle = "#ef3434";
  strokes.forEach((stroke) => drawStroke(ctx, stroke, filled(kind)));
  return new Promise((resolve, reject) => canvas.toBlob(
    (blob) => blob ? resolve(blob) : reject(new Error("无法导出标记图，请重新绘制后重试")), "image/png"));
}

export function RegionalEditDialog({ slot, purpose, onClose, onChanged }: {
  slot: Slot;
  /** "hands": opened from 修手, with the hand instruction and the hand-fix model of Settings. */
  purpose?: "hands";
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const toast = useToast();
  const panel = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const active = useRef<Stroke | null>(null);
  const moving = useRef<CircleMove | null>(null);
  const history = useRef<Stroke[][]>([]);
  const drawingPointer = useRef<number | null>(null);
  const requestId = useRef(crypto.randomUUID());
  const sending = useRef(false);
  const [size, setSize] = useState<Point | null>(null);
  const [strokes, setStrokes] = useState<Stroke[]>([]);
  const [draft, setDraft] = useState<Stroke | null>(null);
  const [movePreview, setMovePreview] = useState<Pick<CircleMove, "index" | "stroke"> | null>(null);
  const [overCircle, setOverCircle] = useState(false);
  const [kind, setKind] = useState<Kind>("crop");
  const [methods, setMethods] = useState<{ id: Kind; name: string; available: boolean; description: string; models: string[] }[]>([]);
  const [tool, setTool] = useState<Tool | "pan">("ellipse");
  const [brush, setBrush] = useState(4);
  const [prompt, setPrompt] = useState(purpose === "hands" ? HAND_INSTRUCTION : "");
  const [providers, setProviders] = useState<ImageProvider[]>([]);
  const [provider, setProvider] = useState<ImageProviderId | "">("");
  const [models, setModels] = useState<Model[] | null>(null);
  const [model, setModel] = useState("");
  const [error, setError] = useState("");
  const [imageError, setImageError] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [analyzing, setAnalyzing] = useState<"detect" | "selection" | null>(null);
  const [analysis, setAnalysis] = useState<HandAnalysis | null>(null);
  const [usedAnalysis, setUsedAnalysis] = useState<HandAnalysis | null>(null);
  const [analysisNotice, setAnalysisNotice] = useState("");
  const [analysisModel, setAnalysisModel] = useState("");
  const [cancelling, setCancelling] = useState<string | null>(null);
  const blockers = usePolledResource(async () => {
    const tasks = await api.listTasks({ target_slot_id: slot.id });
    const superseded = new Set(tasks.map((task) => task.retry_of_task_id).filter(Boolean));
    return tasks.filter((task) => ["waiting", "running", "held"].includes(task.status) && !superseded.has(task.id));
  }, [slot.id], (tasks) => tasks.length > 0);
  const blocked = blockers.data === null || blockers.error !== null || blockers.data.length > 0;
  const cancelInteraction = () => {
    active.current = null;
    moving.current = null;
    drawingPointer.current = null;
    setDraft(null);
    setMovePreview(null);
    setOverCircle(false);
  };
  const viewport = useImageViewport(size, { pan: tool === "pan", disabled: submitting, onNavigate: cancelInteraction });
  const { frame } = viewport;
  const rasterScale = Math.min(viewport.view.scale * Math.min(2, window.devicePixelRatio || 1),
    size ? size.x / frame.x : 1, 4096 / Math.max(frame.x, frame.y));
  const canvasWidth = Math.max(1, Math.round(frame.x * rasterScale));
  const canvasHeight = Math.max(1, Math.round(frame.y * rasterScale));
  const preferred = useRef("");
  const close = useRef(onClose);
  close.current = onClose;
  const source = slot.current_version!;
  const changed = () => { requestId.current = crypto.randomUUID(); setError(""); };
  const invalidateSelection = () => {
    if (usedAnalysis) {
      if (prompt === usedAnalysis.repair_prompts[usedAnalysis.selected_region_index!]) setPrompt(HAND_INSTRUCTION);
      setAnalysisNotice("标记已调整，请重新分析圈选手部。你手动编辑的修改要求会保留。");
    }
    setUsedAnalysis(null);
    setAnalysis((current) => current?.scope === "selection" ? null : current);
  };
  const commit = (next: Stroke[]) => {
    history.current.push(strokes); setStrokes(next); invalidateSelection(); changed();
  };

  useEffect(() => {
    let alive = true;
    api.settings().then((settings) => {
      if (!alive) return;
      const hands = purpose === "hands";
      // A hand fix preselects the hand-fix setting; other edits the regional edit's own (2026-11-13).
      const stage = hands ? "face_fix" : "regional";
      preferred.current = settings[`${stage}_model`] || settings.default_model;
      setProviders(settings.image_providers);
      setAnalysisModel(settings.director_model ?? "未配置");
      setProvider(settings[`${stage}_provider`] || settings.image_provider);
    }).catch((err) => { if (alive) setError(err instanceof Error ? err.message : "无法读取模型设置"); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!provider) return;
    let alive = true;
    setModels(null);
    setModel("");
    setMethods([]);
    api.regionalEditOptions(provider).then(({ models: list, methods: choices }) => {
      if (!alive) return;
      setModels(list);
      setMethods(choices);
      const chosen = list.some((m) => m.id === preferred.current) ? preferred.current : (list[0]?.id ?? "");
      // Nano Banana 2.1 takes only "crop", so a kept method must be one the chosen model can do.
      setKind((value) => choices.some((choice) => choice.id === value && choice.available && choice.models.includes(chosen)) ? value : "crop");
      setModel(chosen);
    }).catch((err) => { if (alive) setError(err instanceof Error ? err.message : "无法读取模型列表"); });
    return () => { alive = false; };
  }, [provider]);

  useEffect(() => {
    const ctx = canvas.current?.getContext("2d");
    if (!ctx || !size) return;
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.strokeStyle = ctx.fillStyle = filled(kind) ? "rgba(239, 52, 52, 0.45)" : "#ef3434";
    const displayed = strokes.map((stroke, index) => movePreview?.index === index ? movePreview.stroke : stroke);
    [...displayed, ...(draft ? [draft] : [])].forEach((stroke) => drawStroke(ctx, stroke, filled(kind)));
  }, [strokes, draft, movePreview, kind, size, canvasWidth, canvasHeight]);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const handle = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !sending.current) close.current();
      if (event.key !== "Tab") return;
      const focusable = Array.from(panel.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), textarea:not(:disabled), select:not(:disabled), input:not(:disabled)') ?? [])
        .filter((item) => item.getClientRects().length > 0);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) {
        event.preventDefault(); first?.focus();
      }
    };
    window.addEventListener("keydown", handle);
    return () => { window.removeEventListener("keydown", handle); previous?.focus(); };
  }, []);

  const point = (event: PointerEvent<HTMLCanvasElement>): Point => {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) };
  };
  const hitCircle = (event: PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const padding = event.pointerType === "touch" ? 12 : 6;
    return circleAt(strokes, point(event), { x: padding / rect.width, y: padding / rect.height });
  };
  const begin = (event: PointerEvent<HTMLCanvasElement>) => {
    if (sending.current || active.current || moving.current || tool === "pan" || viewport.isGestureActive() || event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drawingPointer.current = event.pointerId;
    const index = tool === "ellipse" ? hitCircle(event) : -1;
    if (index >= 0) {
      moving.current = { index, start: point(event), original: strokes[index], stroke: strokes[index] };
      setMovePreview(moving.current);
      return;
    }
    setOverCircle(false);
    active.current = { tool, points: [point(event)], width: tool === "ellipse" ? 0.006 : brush / 100 };
    setDraft(active.current);
  };
  const move = (event: PointerEvent<HTMLCanvasElement>) => {
    if (drawingPointer.current !== event.pointerId || !event.currentTarget.hasPointerCapture(event.pointerId)) {
      if (!sending.current && !active.current && !moving.current && !viewport.isGestureActive()) {
        setOverCircle(tool === "ellipse" && hitCircle(event) >= 0);
      }
      return;
    }
    if (moving.current) {
      moving.current = { ...moving.current, stroke: translateCircle(moving.current, point(event)) };
      setMovePreview(moving.current);
      return;
    }
    if (!active.current) return;
    const stroke = active.current;
    active.current = { ...stroke, points: stroke.tool === "ellipse" ? [stroke.points[0], point(event)] : [...stroke.points, point(event)] };
    setDraft(active.current);
  };
  const end = (event: PointerEvent<HTMLCanvasElement>) => {
    if ((!active.current && !moving.current) || drawingPointer.current !== event.pointerId || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
    move(event);
    const translated = moving.current;
    if (translated) {
      const before = translated.original.points[0], after = translated.stroke.points[0];
      if (before.x !== after.x || before.y !== after.y) {
        commit(strokes.map((stroke, index) => index === translated.index ? translated.stroke : stroke));
      }
    } else if (active.current) {
      const stroke = active.current;
      const first = stroke.points[0], last = stroke.points[stroke.points.length - 1];
      if (stroke.tool === "brush" || (Math.abs(last.x - first.x) > 0.005 && Math.abs(last.y - first.y) > 0.005)) {
        commit([...strokes, stroke]);
      }
    }
    cancelInteraction();
    event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const analyze = async (scope: "detect" | "selection", selection: Stroke[] = strokes, selectionKind: Kind = kind) => {
    if (!size || sending.current || active.current || moving.current || (scope === "selection" && !selection.length)) return;
    sending.current = true;
    setSubmitting(true); setAnalyzing(scope); setError(""); setAnalysisNotice("");
    try {
      const data = new FormData();
      data.set("source_version_id", source.id);
      data.set("annotation_kind", selectionKind);
      if (prompt.trim() !== HAND_INSTRUCTION && (!usedAnalysis || prompt !== usedAnalysis.repair_prompts[usedAnalysis.selected_region_index!])) {
        data.set("instruction", prompt.trim());
      }
      if (scope === "selection") data.set("annotation", await annotationBlob(size, selection, selectionKind), "selection.png");
      const result = await api.analyzeHands(slot.id, data);
      setAnalysis(result);
      setUsedAnalysis(null);
      // A normal or uncertain finding must never silently reuse the preceding diagnosis.
      if (usedAnalysis && prompt === usedAnalysis.repair_prompts[usedAnalysis.selected_region_index!]) setPrompt(HAND_INSTRUCTION);
      if (scope === "selection" && result.status === "repair" && result.regions.length === 1) {
        setPrompt(result.repair_prompts[0]);
        setUsedAnalysis({ ...result, selected_region_index: 0 });
        setAnalysisNotice("已按圈选诊断填入具体修复要求，可编辑后提交。");
      } else if (scope === "selection" && result.status === "reconstruct") {
        setAnalysisNotice("模型提出了局部结构重建方案。请检查具体目标，点击“使用此重建方案”后才会填入修改要求。");
      } else if (scope === "selection" && result.status === "uncertain") {
        setAnalysisNotice("局部核查后仍无法提出有依据的方案。可以扩大范围包含手腕、补充你想修改的具体位置，或直接手动填写要求。");
      }
      changed();
    } catch (err) {
      setError(err instanceof Error ? err.message : "手部分析失败，请重试");
    } finally {
      sending.current = false; setSubmitting(false); setAnalyzing(null);
    }
  };

  const markCandidate = (index: number): Stroke[] => {
    const [x0, y0, x1, y1] = analysis!.regions[index].bbox;
    const next: Stroke[] = [{ tool: "rectangle", points: [{ x: x0, y: y0 }, { x: x1, y: y1 }], width: 0.006 }];
    cancelInteraction(); setKind("crop"); setTool("ellipse"); commit(next);
    return next;
  };

  const reviewCandidate = (index: number) => {
    if (!analysis || analysis.scope !== "detect" || !analysis.regions[index] || sending.current) return;
    const next = markCandidate(index);
    // Pass the new region directly: React state still contains the preceding selection here.
    void analyze("selection", next, "crop");
  };

  const useCandidate = (index: number) => {
    if (!analysis || sending.current || !analysis.regions[index] || analysis.regions[index].status === "uncertain") return;
    if (analysis.scope === "detect") markCandidate(index);
    setPrompt(analysis.repair_prompts[index]);
    setUsedAnalysis({ ...analysis, selected_region_index: index });
    changed();
    setAnalysisNotice(analysis.regions[index].status === "reconstruct"
      ? "已采用建议重建方案并填入修改要求，这不代表原图缺陷已被确认。检查范围和目标后再提交修图。"
      : "已圈选此处并填入修复要求。检查红色范围是否覆盖整只手及指间空隙；可移动、撤销或重画，调整后请重新分析。");
  };

  const submit = async () => {
    if (!size || !strokes.length || !prompt.trim() || !provider || !model || sending.current || active.current || moving.current || blocked || cancelling) return;
    sending.current = true;
    setSubmitting(true);
    setError("");
    try {
      const blob = await annotationBlob(size, strokes, kind);
      const data = new FormData();
      data.set("client_request_id", requestId.current);
      data.set("source_version_id", source.id);
      data.set("prompt", prompt.trim());
      data.set("provider", provider);
      data.set("model", model);
      if (usedAnalysis) data.set("hand_analysis", JSON.stringify(usedAnalysis));
      data.set("method", kind === "reference" || kind === "crop" ? kind : "native");
      data.set(kind === "reference" || kind === "crop" ? "markup" : kind, blob, `${kind}.png`);
      await api.regionalEdit(slot.id, data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "局部编辑提交失败，请重试");
      sending.current = false;
      setSubmitting(false);
      if (err instanceof ApiError && err.code === "slot_busy") await blockers.refresh();
      return;
    }
    toast.info("已提交局部编辑任务，原图保留在历史版本中");
    onClose();
    try { await onChanged(); } catch (err) { toast.error(err); }
  };

  const cancelPausedTask = async (id: string) => {
    if (cancelling || sending.current) return;
    setCancelling(id);
    setError("");
    try {
      // Check the state atomically on the server so a resumed request is never stopped by this button.
      await api.cancelTask(id, "held");
      toast.info("已取消该暂停任务，当前照片和标记保留");
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消暂停任务失败，请重试");
    } finally {
      await blockers.refresh();
      setCancelling(null);
    }
    try { await onChanged(); } catch (err) { toast.error(err); }
  };

  return (
    <div className="backdrop regional-edit-backdrop" onClick={() => { if (!sending.current) onClose(); }}>
      <div ref={panel} className="card modal regional-edit-modal" role="dialog" aria-modal="true"
        aria-labelledby="regional-edit-title" tabIndex={-1} onClick={(event) => event.stopPropagation()}>
        <div className="regional-edit-heading">
          <div><h2 id="regional-edit-title">{purpose === "hands" ? "修手" : "局部编辑"}</h2>
            <p>{purpose === "hands" ? "圈选后分析具体问题，或让 AI 先找出疑似问题手；检查修复要求后再提交。" : "标出需要修改的位置，再描述修改内容。"}</p></div>
          <button className="btn btn-ghost" onClick={onClose} disabled={submitting} aria-label="关闭局部编辑">关闭</button>
        </div>
        {blockers.data === null && !blockers.error && <p className="field-hint" role="status">正在检查这张照片的任务状态…</p>}
        {blockers.error && <div className="regional-edit-task-notice" role="alert">
          <p>无法读取任务状态，请重试后再提交。当前照片和标记会保留。</p>
          <button className="btn btn-sm" onClick={() => blockers.refresh()}>重新检查任务</button>
        </div>}
        {blockers.data && blockers.data.length > 0 && <div className="regional-edit-task-notice" role="status">
          <p>{blockers.data.some((task) => task.status !== "held")
            ? "这张照片还有排队或生成中的任务。你可以继续调整标记，任务结束后再提交。"
            : "这张照片还有已暂停的任务，暂停后不会自动完成。取消下方暂停任务即可提交编辑，或关闭编辑器后到任务中心继续原任务。"}</p>
          {blockers.data.map((task) => <div key={task.id} className="regional-edit-task-row">
            <span>{TASK_STATUS_LABEL[task.status]} · {new Date(task.created_at).toLocaleString("zh-CN")}</span>
            {task.status === "held" && <button className="btn btn-sm" disabled={!!cancelling || submitting}
              onClick={() => cancelPausedTask(task.id)}>{cancelling === task.id ? "正在取消…" : "取消此暂停任务"}</button>}
          </div>)}
          <span className="field-hint">取消暂停任务不会删除当前照片或你画好的标记，也不会自动提交新的付费编辑。</span>
        </div>}
        <div className="regional-edit-layout">
          <div className="regional-edit-workspace">
            <div className="regional-edit-toolbar" aria-label="标记工具">
              <button className={`btn btn-sm ${tool === "ellipse" ? "btn-primary" : ""}`} aria-pressed={tool === "ellipse"}
                disabled={submitting} onClick={() => setTool("ellipse")}>画圈</button>
              <button className={`btn btn-sm ${tool === "brush" ? "btn-primary" : ""}`} aria-pressed={tool === "brush"}
                disabled={submitting} onClick={() => setTool("brush")}>画笔</button>
              <button className={`btn btn-sm ${tool === "pan" ? "btn-primary" : ""}`} aria-pressed={tool === "pan"}
                disabled={submitting} onClick={() => setTool("pan")}>移动照片</button>
              {tool === "brush" && <label className="regional-edit-brush">粗细<input aria-label="画笔粗细" type="range"
                min={1} max={12} value={brush} disabled={submitting} onChange={(event) => setBrush(Number(event.target.value))} /></label>}
              <button className="btn btn-sm btn-ghost" disabled={!history.current.length || submitting || draft !== null || movePreview !== null}
                onClick={() => { const previous = history.current.pop(); if (previous) { setStrokes(previous); invalidateSelection(); changed(); } }}>撤销</button>
              <button className="btn btn-sm btn-ghost" disabled={!strokes.length || submitting}
                onClick={() => { cancelInteraction(); commit([]); }}>清空</button>
            </div>
            <ImageZoomControls viewport={viewport} disabled={submitting || !size} />
            <div ref={viewport.stage} className={`regional-edit-stage ${tool === "pan" ? "is-move-tool" : ""} ${viewport.navigating ? "is-panning" : ""}`}
              {...viewport.handlers}>
              <div className="regional-edit-image" style={{ width: size ? frame.x : "100%", height: size ? frame.y : "100%", transform: viewport.transform }}>
                <img src={assetUrl(source.asset_id)} alt="待编辑的原照片" draggable={false}
                  onLoad={(event) => setSize({ x: event.currentTarget.naturalWidth, y: event.currentTarget.naturalHeight })}
                  onError={() => setImageError(true)} />
                {size && <canvas ref={canvas} width={canvasWidth}
                  height={canvasHeight} aria-label={tool === "pan" ? "拖动移动放大后的照片" : "在照片上拖动画圈或涂抹需要编辑的区域"}
                  style={{ cursor: tool === "ellipse" ? movePreview ? "grabbing" : overCircle ? "grab" : undefined : undefined }}
                  onPointerDown={begin} onPointerMove={move} onPointerUp={end}
                  onPointerLeave={() => setOverCircle(false)}
                  onPointerCancel={cancelInteraction} onLostPointerCapture={(event) => {
                    if (drawingPointer.current === event.pointerId) cancelInteraction();
                  }} />}
              </div>
              {!size && <span className="regional-edit-loading">{imageError ? "原图加载失败，请关闭后重试" : "正在加载原图…"}</span>}
            </div>
            <span className="field-hint">画圈模式下，拖动已有圈的内部或边缘调整位置，从圈外开始画新圈。选择“移动照片”可拖动放大后的照片；滚轮或双指缩放，比例以适应窗口为 100%。</span>
            <span className="field-hint">{kind === "crop" ? "红色覆盖的区域会被修改并贴回原图，其余部分不变；圈要把整只手（连手指）都盖住，可以稍大一点。"
              : kind === "mask" ? "红色覆盖的区域允许修改；圈选会填满圈内，画笔可涂抹细节。" : "圈住要改的对象，干净的原图会保留。"}</span>
          </div>
          <div className="regional-edit-options">
            <section className="hand-analysis-panel" aria-label="手部诊断">
              <strong>手部诊断</strong>
              <span className="field-hint">使用 AI 导演模型：{analysisModel || "正在读取…"}，需支持看图。每次分析单独计文字模型费用，不会自动提交修图。</span>
              <div className="hand-analysis-actions">
                <button className="btn btn-sm" disabled={!size || !strokes.length || submitting || draft !== null || movePreview !== null}
                  onClick={() => analyze("selection")}>{analyzing === "selection" ? "正在分析…" : "分析圈选手部"}</button>
                <button className="btn btn-sm" disabled={!size || submitting || draft !== null || movePreview !== null}
                  onClick={() => analyze("detect")}>{analyzing === "detect" ? "正在查找…" : "自动找问题手（实验）"}</button>
              </div>
              <span className="field-hint">整图查找只作初筛；疑似区域可“圈选并放大分析”，会再产生一次文字模型请求。局部结构含糊时可提供建议重建方案，由你选择是否采用。</span>
              {analyzing && <p className="field-hint" role="status">正在看图诊断，可能需要约一分钟，请等待本次请求完成。</p>}
              {analysis && <div className="hand-analysis-result" aria-live="polite">
                <p><strong>{analysis.status === "repair" ? "发现可修复问题" : analysis.status === "reconstruct" ? "可尝试局部结构重建" : analysis.status === "normal" ? "未发现明确问题" : analysis.scope === "detect" && analysis.regions.length ? "需要放大核查" : "暂时无法制定方案"}</strong>：{analysis.summary}</p>
                <span className="field-hint">本次分析：{analysis.provider === "pptoken" ? "PPTOKEN" : "88API"} · {analysis.model}</span>
                {analysis.regions.map((region, index) => <div className="hand-analysis-region" key={index}>
                  <strong>{region.label}{region.status === "reconstruct" ? " · 建议重建" : region.status === "uncertain" ? analysis.scope === "detect" ? " · 待放大核查" : " · 无法制定方案" : ""}</strong>
                  {region.evidence.length > 0 && <p>可见证据：{region.evidence.join("；")}</p>}
                  {region.problems.length > 0 && <p>{region.status === "reconstruct" ? "需要理顺" : "具体问题"}：{region.problems.join("；")}</p>}
                  {region.goal && <p>{region.status === "reconstruct" ? "建议重建" : "修复目标"}：{region.goal}</p>}
                  {region.preserve.length > 0 && <p>保留：{region.preserve.join("；")}</p>}
                  {region.uncertainties.length > 0 && <p>无法确认：{region.uncertainties.join("；")}</p>}
                  {analysis.scope === "detect" && <button className="btn btn-sm" disabled={submitting}
                    onClick={() => reviewCandidate(index)}>圈选并放大分析</button>}
                  {analysis.scope === "detect" && region.status === "repair" && <button className="btn btn-sm" disabled={submitting}
                    onClick={() => useCandidate(index)}>{usedAnalysis?.selected_region_index === index && usedAnalysis.scope === "detect" ? "重新使用此圈选" : "圈选此处并填入修复要求"}</button>}
                  {region.status === "reconstruct" && <button className="btn btn-sm" disabled={submitting}
                    onClick={() => useCandidate(index)}>{analysis.scope === "detect" ? "圈选并使用此重建方案" : "使用此重建方案"}</button>}
                </div>)}
              </div>}
              {analysisNotice && <p className="field-hint" role="status">{analysisNotice}</p>}
            </section>
            <label className="field"><span className="field-label">编辑方式</span>
              <select className="input" value={kind} disabled={submitting || !methods.length} onChange={(event) => {
                cancelInteraction(); history.current = []; setKind(event.target.value as Kind); setStrokes([]); invalidateSelection(); changed();
              }}>{!methods.length && <option value="crop">正在加载…</option>}
                {methods.map((choice) => <option key={choice.id} value={choice.id}
                  disabled={!choice.available || (!!model && !choice.models.includes(model))}>{choice.name}</option>)}
              </select>
              <span className="field-hint">{methods.find((choice) => choice.id === kind)?.description}</span>
              <span className="field-hint">切换方式会清空当前标记。</span>
            </label>
            <label className="field"><span className="field-label">修改要求</span>
              <textarea className="input textarea" rows={5} maxLength={2000} value={prompt} disabled={submitting}
                placeholder="例如：修正圈内粘连的手指，保持牵手动作和戒指。"
                onChange={(event) => { setPrompt(event.target.value); changed(); }} />
            </label>
            <button className="btn btn-sm" disabled={submitting} onClick={() => {
              setPrompt(HAND_INSTRUCTION); setUsedAnalysis(null); setAnalysisNotice("已切换为通用要求，本次提交不关联诊断。"); changed();
            }}>填入通用修手要求</button>
            <label className="field"><span className="field-label">图片服务</span>
              <select className="input" value={provider} disabled={submitting || !providers.length}
                onChange={(event) => { setProvider(event.target.value as ImageProviderId); changed(); }}>
                {!providers.length && <option value="">正在加载…</option>}
                {providers.map((item) => <option key={item.id} value={item.id} disabled={!item.available}>{item.name}</option>)}
              </select>
            </label>
            <label className="field"><span className="field-label">本次使用的模型</span>
              <select className="input" value={model} disabled={submitting || !models?.length}
                onChange={(event) => { setModel(event.target.value); changed(); }}>
                {!models && <option value="">正在加载…</option>}
                {models?.length === 0 && <option value="">没有可用的局部编辑模型</option>}
                {models?.map((item) => {
                  const unsupported = methods.length > 0 && !methods.find((choice) => choice.id === kind)?.models.includes(item.id);
                  return <option key={item.id} value={item.id} disabled={unsupported}>{item.name}{unsupported ? "（仅支持放大局部修）" : ""}</option>;
                })}
              </select>
            </label>
            <p className="field-hint">{kind === "crop"
              ? "共 1 个付费编辑任务：只发送标记区域连同周围放大成的 1024×1024 小图，修好后只把标记区域贴回原图，原图尺寸和标记以外的部分都不变。原图保留，可在历史版本中对比和恢复。"
              : "共 1 个付费编辑任务，中等质量，自动选择输出尺寸，PNG 格式。整张图会被重画，未选中的细节也可能变化。原图保留，可在历史版本中对比和恢复。"}</p>
          </div>
        </div>
        {error && <p className="error-text" role="alert">{error}</p>}
        <div className="modal-actions">
          <button className="btn btn-ghost" disabled={submitting} onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={!size || !strokes.length || !prompt.trim() || !model || submitting || draft !== null || movePreview !== null || blocked || !!cancelling}
            onClick={submit}>{analyzing ? "正在分析…" : submitting ? "正在提交…" : "提交局部编辑"}</button>
        </div>
      </div>
    </div>
  );
}

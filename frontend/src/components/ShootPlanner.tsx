import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  ORIENTATION_LABEL,
  ORIENTATIONS,
  SHOT_LABEL,
  SHOT_SIZES,
  type Model,
  type Moment,
  type MomentDraft,
  type ShotSize,
  type Slot,
} from "../api";

const PHOTO_COUNTS = [4, 8, 12, 20];

type Phase = "setup" | "writing" | "review" | "shooting";

/**
 * "用这个风格拍一组": the AI director writes N moments (cheap text call), the user
 * reviews / swaps / edits them, and only then are the paid image tasks created.
 */
export function ShootPlanner({
  projectId,
  preview,
  shotCount,
  onClose,
  onShot,
}: {
  projectId: string;
  preview: Slot;
  shotCount: number;
  onClose: () => void;
  onShot: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("setup");
  const [count, setCount] = useState(8);
  const [models, setModels] = useState<Model[] | null>(null);
  const [model, setModel] = useState("");
  const [draft, setDraft] = useState<MomentDraft | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Index of the moment currently being rewritten (one at a time keeps it simple).
  const [rewriting, setRewriting] = useState<number | null>(null);

  useEffect(() => {
    Promise.all([api.models(), api.settings()])
      .then(([list, settings]) => {
        setModels(list);
        setModel(settings.default_model);
      })
      .catch(() => setError("无法读取模型列表，请确认后端已启动"));
  }, []);

  const message = (err: unknown) => (err instanceof ApiError ? err.message : "操作失败，请稍后重试");

  const write = async () => {
    setError(null);
    setPhase("writing");
    try {
      setDraft(await api.createDraft(projectId, preview.id, count));
      setPhase("review");
    } catch (err) {
      setError(message(err));
      setPhase("setup");
    }
  };

  const rewrite = async (index: number, shot?: ShotSize) => {
    if (!draft) return;
    setError(null);
    setRewriting(index);
    try {
      setDraft(await api.rewriteMoment(draft.id, index, shot));
    } catch (err) {
      setError(message(err));
    } finally {
      setRewriting(null);
    }
  };

  const save = async (index: number, patch: Partial<Moment>) => {
    if (!draft) return false;
    setError(null);
    try {
      setDraft(await api.updateMoment(draft.id, index, patch));
      return true;
    } catch (err) {
      setError(message(err));
      return false;
    }
  };

  const shoot = async () => {
    if (!draft) return;
    setError(null);
    setPhase("shooting");
    try {
      await api.createPhotoBatch(projectId, draft.id, model);
      onShot();
    } catch (err) {
      setError(message(err));
      setPhase("review");
    }
  };

  const selectedModel = models?.find((m) => m.id === model);
  const shots = draft ? SHOT_SIZES.map((s) => [s, draft.moments.filter((m) => m.shot_size === s).length] as const) : [];
  const landscapeCount = draft ? draft.moments.filter((m) => m.orientation === "landscape").length : 0;

  return (
    <div className="backdrop" onClick={phase === "writing" || phase === "shooting" ? undefined : onClose}>
      <div className="card modal planner" onClick={(e) => e.stopPropagation()}>
        <div className="planner-header">
          <div>
            <h2>用「{preview.style_name}」拍一组</h2>
            <p>
              {phase === "review"
                ? "AI 导演写好了这一组的拍摄瞬间。不喜欢的可以换一个、改景别或直接修改文字，确认后才开始生成图片。"
                : "先由 AI 导演按这个风格的场景设计每一张的动作和景别（只花很少的文字费用），你确认后再生成图片。"}
              {shotCount > 0 && phase !== "review" && ` 这个风格已经拍了 ${shotCount} 张，新的一组会避开已拍过的动作。`}
            </p>
          </div>
          {phase !== "writing" && phase !== "shooting" && (
            <button className="btn btn-ghost btn-sm" onClick={onClose}>
              关闭
            </button>
          )}
        </div>

        {error && <div className="notice notice-error">{error}</div>}

        {(phase === "setup" || phase === "writing") && (
          <>
            <div className="field">
              <span className="field-label">生成数量</span>
              <div className="count-options">
                {PHOTO_COUNTS.map((n) => (
                  <button
                    key={n}
                    type="button"
                    className={`count-option ${count === n ? "active" : ""}`}
                    onClick={() => setCount(n)}
                    disabled={phase === "writing"}
                  >
                    {n}
                  </button>
                ))}
              </div>
            </div>
            <ModelField models={models} model={model} onChange={setModel} disabled={phase === "writing"} hint={selectedModel?.description} />
            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={onClose} disabled={phase === "writing"}>
                取消
              </button>
              <button className="btn btn-primary" onClick={write} disabled={phase === "writing" || !model}>
                {phase === "writing" ? (
                  <>
                    <span className="spinner spinner-sm spinner-light" /> AI 导演正在设计 {count} 个瞬间…
                  </>
                ) : (
                  "让 AI 导演设计这一组"
                )}
              </button>
            </div>
          </>
        )}

        {(phase === "review" || phase === "shooting") && draft && (
          <>
            <div className="planner-summary">
              {shots
                .filter(([, n]) => n > 0)
                .map(([s, n]) => (
                  <span key={s} className="tag">
                    {SHOT_LABEL[s]} {n}
                  </span>
                ))}
              <span className="tag">
                竖版 {draft.moments.length - landscapeCount} · 横版 {landscapeCount}
              </span>
              {draft.director_model && <span className="muted">由 {draft.director_model} 设计</span>}
            </div>

            <div className="moment-list">
              {draft.moments.map((m, i) => (
                <MomentRow
                  key={`${draft.id}-${i}-${m.title}`}
                  index={i}
                  moment={m}
                  busy={rewriting === i}
                  locked={rewriting !== null || phase === "shooting"}
                  onRewrite={(shot) => rewrite(i, shot)}
                  onSave={(patch) => save(i, patch)}
                />
              ))}
            </div>

            <ModelField models={models} model={model} onChange={setModel} disabled={phase === "shooting"} hint={selectedModel?.description} />

            <div className="modal-actions planner-actions">
              <span className="field-hint">
                将创建 {draft.moments.length} 个图片生成任务，会产生相应费用。
              </span>
              <button className="btn btn-ghost" onClick={onClose} disabled={phase === "shooting"}>
                取消
              </button>
              <button
                className="btn btn-primary"
                onClick={shoot}
                disabled={phase === "shooting" || rewriting !== null || !model}
              >
                {phase === "shooting" ? "正在提交…" : `开始拍摄 ${draft.moments.length} 张`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ModelField({
  models,
  model,
  onChange,
  disabled,
  hint,
}: {
  models: Model[] | null;
  model: string;
  onChange: (id: string) => void;
  disabled: boolean;
  hint?: string;
}) {
  return (
    <label className="field">
      <span className="field-label">图片模型</span>
      <select className="input" value={model} onChange={(e) => onChange(e.target.value)} disabled={!models || disabled}>
        {models?.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

function MomentRow({
  index,
  moment,
  busy,
  locked,
  onRewrite,
  onSave,
}: {
  index: number;
  moment: Moment;
  busy: boolean;
  locked: boolean;
  onRewrite: (shot?: ShotSize) => void;
  onSave: (patch: Partial<Moment>) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(moment.title);
  const [text, setText] = useState(moment.text);

  const commit = async () => {
    const ok = await onSave({ title: title.trim(), text: text.trim() });
    if (ok) setEditing(false);
  };

  return (
    <div className={`moment ${busy ? "moment-busy" : ""}`}>
      <div className="moment-index">{String(index + 1).padStart(2, "0")}</div>
      <div className="moment-body">
        {editing ? (
          <>
            <input className="input" value={title} maxLength={12} onChange={(e) => setTitle(e.target.value)} />
            <textarea className="input textarea" value={text} maxLength={220} rows={5} onChange={(e) => setText(e.target.value)} />
            <div className="moment-actions">
              <button className="btn btn-sm btn-primary" onClick={commit} disabled={!title.trim() || !text.trim()}>
                保存
              </button>
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => {
                  setTitle(moment.title);
                  setText(moment.text);
                  setEditing(false);
                }}
              >
                取消
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="moment-title">
              <h3>{moment.title}</h3>
              {busy && <span className="spinner spinner-sm" />}
            </div>
            <p className="moment-text">{moment.text}</p>
            <div className="moment-actions">
              <div className="shot-switch" role="group" aria-label="景别">
                {SHOT_SIZES.map((s) => (
                  <button
                    key={s}
                    className={`shot-pill ${moment.shot_size === s ? "active" : ""}`}
                    disabled={locked || moment.shot_size === s}
                    title={moment.shot_size === s ? "当前景别" : `保持动作，改成${SHOT_LABEL[s]}`}
                    onClick={() => onRewrite(s)}
                  >
                    {SHOT_LABEL[s]}
                  </button>
                ))}
              </div>
              <div className="shot-switch" role="group" aria-label="画幅">
                {ORIENTATIONS.map((o) => (
                  <button
                    key={o}
                    className={`shot-pill ${moment.orientation === o ? "active" : ""}`}
                    disabled={locked || moment.orientation === o}
                    title={moment.orientation === o ? "当前画幅" : `改成${ORIENTATION_LABEL[o]}，描述文字不变`}
                    onClick={() => onSave({ orientation: o })}
                  >
                    {ORIENTATION_LABEL[o]}
                  </button>
                ))}
              </div>
              <span className="selection-spacer" />
              <button className="btn btn-sm btn-ghost" onClick={() => onRewrite()} disabled={locked}>
                换一个
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => setEditing(true)} disabled={locked}>
                修改
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

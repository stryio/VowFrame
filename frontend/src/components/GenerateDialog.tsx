import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, SHOT_LABEL, SHOT_SIZES, type Model, type ShotSize, type StoredShotSize } from "../api";

interface GenerateRequest {
  title: string;
  message: ReactNode;
  /** Fixed number of tasks, or a list of choices (first default = `defaultCount`). */
  taskCount: number | { options: number[]; defaultCount: number };
  confirmText?: string;
  /** Ask for a target shot size; `current` (if any) cannot be chosen. */
  shotChoice?: { current: StoredShotSize | null };
}

export interface GenerateChoice {
  model: string;
  count: number;
  shot?: ShotSize;
}

/**
 * Confirmation for any action that creates paid generation tasks (PRD 15.6).
 * Shows the task count and lets the user override the default model for this run.
 */
export function useGenerateDialog() {
  const [pending, setPending] = useState<{
    request: GenerateRequest;
    resolve: (choice: GenerateChoice | null) => void;
  } | null>(null);

  const ask = useCallback(
    (request: GenerateRequest) =>
      new Promise<GenerateChoice | null>((resolve) => setPending({ request, resolve })),
    [],
  );

  const close = (choice: GenerateChoice | null) => {
    pending?.resolve(choice);
    setPending(null);
  };

  const dialog = pending ? (
    <GenerateDialog request={pending.request} onConfirm={close} onCancel={() => close(null)} />
  ) : null;

  return { ask, dialog };
}

function GenerateDialog({
  request,
  onConfirm,
  onCancel,
}: {
  request: GenerateRequest;
  onConfirm: (choice: GenerateChoice) => void;
  onCancel: () => void;
}) {
  const countChoice = typeof request.taskCount === "number" ? null : request.taskCount;
  const [count, setCount] = useState(
    typeof request.taskCount === "number" ? request.taskCount : request.taskCount.defaultCount,
  );
  const [shot, setShot] = useState<ShotSize | undefined>(
    request.shotChoice ? SHOT_SIZES.find((s) => s !== request.shotChoice!.current) : undefined,
  );
  const [models, setModels] = useState<Model[] | null>(null);
  const [model, setModel] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    Promise.all([api.models(), api.settings()])
      .then(([list, settings]) => {
        setModels(list);
        setModel(settings.default_model);
      })
      .catch(() => setFailed(true));
  }, []);

  const selected = models?.find((m) => m.id === model);

  return (
    <div className="backdrop" onClick={onCancel}>
      <div className="card modal" onClick={(e) => e.stopPropagation()}>
        <h2>{request.title}</h2>
        <p>{request.message}</p>

        {countChoice && (
          <div className="field">
            <span className="field-label">生成数量</span>
            <div className="count-options">
              {countChoice.options.map((n) => (
                <button
                  key={n}
                  type="button"
                  className={`count-option ${count === n ? "active" : ""}`}
                  onClick={() => setCount(n)}
                >
                  {n}
                </button>
              ))}
            </div>
          </div>
        )}

        {request.shotChoice && (
          <div className="field">
            <span className="field-label">目标景别</span>
            <div className="count-options">
              {SHOT_SIZES.map((s) => (
                <button
                  key={s}
                  type="button"
                  className={`count-option shot-option ${shot === s ? "active" : ""}`}
                  disabled={s === request.shotChoice!.current}
                  title={s === request.shotChoice!.current ? "当前景别" : undefined}
                  onClick={() => setShot(s)}
                >
                  {SHOT_LABEL[s]}
                </button>
              ))}
            </div>
          </div>
        )}

        <label className="field">
          <span className="field-label">本次使用的模型</span>
          {failed ? (
            <span className="error-text">无法读取模型列表，请确认后端已启动</span>
          ) : (
            <select
              className="input"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={!models}
            >
              {models?.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          )}
          {selected && <span className="field-hint">{selected.description}</span>}
        </label>

        <p className="field-hint">
          共创建 {count} 个生成任务，会产生相应费用。默认模型可在「设置」中修改。
        </p>

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className="btn btn-primary"
            onClick={() => onConfirm({ model, count, shot })}
            disabled={!model || (request.shotChoice !== undefined && !shot)}
            autoFocus
          >
            {request.confirmText ?? "开始生成"}
          </button>
        </div>
      </div>
    </div>
  );
}

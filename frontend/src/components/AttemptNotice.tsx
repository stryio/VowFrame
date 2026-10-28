import { isBroken, isGenerating, type Slot } from "../api";

/**
 * Shown under a photo that already has a version while a newer attempt is running or
 * has failed. The old image stays visible on failure (PRD 4.4), so without this strip a
 * failed regeneration looks like nothing happened.
 */
export function AttemptNotice({
  slot,
  busy,
  onRetry,
}: {
  slot: Slot;
  busy: boolean;
  onRetry: () => void;
}) {
  const task = slot.latest_task;
  if (!slot.current_version || !task) return null;

  const swap = task.regenerate_mode === "face_swap";
  const model = task.model && !swap ? `用 ${task.model} ` : "";
  const action = swap ? "换脸修正" : "重新生成";

  if (isGenerating(slot)) {
    return (
      <div className="attempt attempt-running">
        <div className="spinner spinner-sm" />
        <span>
          {model}
          {task.status === "running" ? `${action}中` : `排队等待${action}`}，完成前仍显示当前版本
        </span>
      </div>
    );
  }

  if (isBroken(slot)) {
    return (
      <div className="attempt attempt-failed">
        <div className="attempt-text">
          <strong>{model}{action}{task.status === "interrupted" ? "已中断" : "失败"}</strong>，仍显示原来的版本。
          {task.error_message && <div className="attempt-reason">{task.error_message}</div>}
        </div>
        <button className="btn btn-sm" onClick={onRetry} disabled={busy}>
          重试
        </button>
      </div>
    );
  }

  return null;
}

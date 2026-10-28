import { useState } from "react";
import {
  api,
  BATCH_STATUS_LABEL,
  BATCH_TYPE_LABEL,
  TASK_STATUS_LABEL,
  VERSION_MODE_LABEL,
  type Batch,
  type Task,
} from "../api";
import { usePolledResource } from "../hooks";
import { EmptyState, useConfirm, useToast } from "./feedback";

const hasActive = (batches: Batch[]) =>
  batches.some((b) => b.counts.waiting + b.counts.running > 0);

export function TaskDrawer({
  projectId,
  onClose,
  onChanged,
}: {
  projectId: string | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const { confirm, dialog } = useConfirm();
  const [expanded, setExpanded] = useState<string | null>(null);
  const batches = usePolledResource(
    () => (projectId ? api.listBatches(projectId) : Promise.resolve([])),
    [projectId],
    hasActive,
  );

  const act = async (run: () => Promise<unknown>, done?: string) => {
    try {
      await run();
      if (done) toast.info(done);
      await batches.refresh();
      onChanged();
    } catch (err) {
      toast.error(err);
    }
  };

  const cancelRest = async (batch: Batch) => {
    const ok = await confirm({
      title: "取消剩余任务",
      message: "将保留已完成的照片，尚未开始的任务将被取消。正在生成的图片会继续完成。",
      confirmText: "取消剩余",
      danger: true,
    });
    if (ok) await act(() => api.cancelBatch(batch.id), "已取消剩余任务");
  };

  const retryAll = async (batch: Batch) => {
    const tasks = await api.listTasks({ batch_id: batch.id });
    const targets = tasks.filter((t) => t.status === "failed" || t.status === "interrupted");
    // Face swaps run locally, so only paid generation tasks get the cost warning.
    const paid = targets.filter((t) => t.regenerate_mode !== "face_swap").length;
    const ok = await confirm({
      title: "重试失败任务",
      message:
        paid > 0
          ? `将重新创建 ${targets.length} 个任务，其中 ${paid} 个生成任务会产生相应费用。`
          : `将重新提交 ${targets.length} 个换脸修正任务，本地处理、不产生费用。`,
      confirmText: "重试",
    });
    if (ok) await act(() => Promise.all(targets.map((t) => api.retryTask(t.id))), "已重新提交");
  };

  return (
    <div className="backdrop drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-header">
          <h2>任务中心</h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="drawer-body">
          {!projectId ? (
            <EmptyState icon="◷" title="尚未打开项目" description="打开一个项目后可在这里查看它的生成任务。" />
          ) : batches.data === null ? (
            <div className="muted">加载中…</div>
          ) : batches.data.length === 0 ? (
            <EmptyState icon="◷" title="暂无任务" description="开始生成后，任务进度会显示在这里。" />
          ) : (
            batches.data.map((batch) => (
              <BatchCard
                key={batch.id}
                batch={batch}
                expanded={expanded === batch.id}
                onToggle={() => setExpanded(expanded === batch.id ? null : batch.id)}
                onContinue={() => act(() => api.continueBatch(batch.id), "已继续剩余任务")}
                onCancel={() => cancelRest(batch)}
                onRetryAll={() => retryAll(batch)}
                onTaskAction={act}
              />
            ))
          )}
        </div>
      </aside>
      {dialog}
    </div>
  );
}

function BatchCard({
  batch,
  expanded,
  onToggle,
  onContinue,
  onCancel,
  onRetryAll,
  onTaskAction,
}: {
  batch: Batch;
  expanded: boolean;
  onToggle: () => void;
  onContinue: () => void;
  onCancel: () => void;
  onRetryAll: () => void;
  onTaskAction: (run: () => Promise<unknown>, done?: string) => Promise<void>;
}) {
  const c = batch.counts;
  const pending = c.waiting + c.held;
  const broken = c.failed + c.interrupted;
  const pct = (n: number) => `${batch.total ? (n / batch.total) * 100 : 0}%`;

  return (
    <div className="card batch-card">
      <div className="batch-card-header">
        <h3>{BATCH_TYPE_LABEL[batch.batch_type]}</h3>
        <span
          className={`tag ${
            batch.status === "needs_attention" || batch.status === "held"
              ? "tag-alert"
              : batch.status === "running"
                ? "tag-gold"
                : ""
          }`}
        >
          {BATCH_STATUS_LABEL[batch.status]}
        </span>
      </div>

      {/* Real counts only; no fake percentage or ETA (PRD 9.4). */}
      <div>
        已完成 {c.completed} / {batch.total} 张
      </div>
      <div className="progress">
        <div className="progress-done" style={{ width: pct(c.completed) }} />
        <div className="progress-failed" style={{ width: pct(broken) }} />
      </div>
      <div className="batch-counts">
        {c.running > 0 && <span>生成中 {c.running}</span>}
        {c.waiting > 0 && <span>等待中 {c.waiting}</span>}
        {c.held > 0 && <span>已搁置 {c.held}</span>}
        {c.failed > 0 && <span>失败 {c.failed}</span>}
        {c.interrupted > 0 && <span>已中断 {c.interrupted}</span>}
        {c.cancelled > 0 && <span>已取消 {c.cancelled}</span>}
        <span>{new Date(batch.created_at).toLocaleString("zh-CN")}</span>
      </div>

      {batch.status === "held" && (
        <div className="notice">
          这些任务尚未发出请求，需要你确认后才会继续（例如服务重启后或账户异常后）。
        </div>
      )}
      {c.interrupted > 0 && (
        <div className="notice">
          有 {c.interrupted} 个请求的结果无法确认，可能已经扣费。重新生成会产生额外费用。
        </div>
      )}

      <div className="batch-actions">
        {c.held > 0 && (
          <button className="btn btn-gold btn-sm" onClick={onContinue}>
            继续剩余
          </button>
        )}
        {broken > 0 && (
          <button className="btn btn-sm" onClick={onRetryAll}>
            重试失败（{broken}）
          </button>
        )}
        {pending > 0 && (
          <button className="btn btn-danger btn-sm" onClick={onCancel}>
            取消剩余
          </button>
        )}
        <button className="btn btn-ghost btn-sm" onClick={onToggle}>
          {expanded ? "收起" : "查看任务"}
        </button>
      </div>

      {expanded && <TaskList batchId={batch.id} onTaskAction={onTaskAction} />}
    </div>
  );
}

function TaskList({
  batchId,
  onTaskAction,
}: {
  batchId: string;
  onTaskAction: (run: () => Promise<unknown>, done?: string) => Promise<void>;
}) {
  const tasks = usePolledResource(
    () => api.listTasks({ batch_id: batchId }),
    [batchId],
    (list: Task[]) => list.some((t) => t.status === "waiting" || t.status === "running"),
  );

  if (!tasks.data) return <div className="muted">加载中…</div>;

  const run = async (fn: () => Promise<unknown>, done: string) => {
    await onTaskAction(fn, done);
    await tasks.refresh();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {tasks.data.map((t) => (
        <div key={t.id} className="task-row">
          <div>
            <div>
              {TASK_STATUS_LABEL[t.status]}
              {t.regenerate_mode && <span className="muted"> · {VERSION_MODE_LABEL[t.regenerate_mode]}</span>}
              {t.model && <span className="muted"> · {t.model}</span>}
            </div>
            {t.error_message && <div className="error-text">{t.error_message}</div>}
          </div>
          {(t.status === "failed" || t.status === "interrupted") && (
            <button className="btn btn-sm" onClick={() => run(() => api.retryTask(t.id), "已重新提交")}>
              重试
            </button>
          )}
          {(t.status === "waiting" || t.status === "held") && (
            <button className="btn btn-ghost btn-sm" onClick={() => run(() => api.cancelTask(t.id), "已取消")}>
              取消
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

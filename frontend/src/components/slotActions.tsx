import { useEffect, useRef, useState } from "react";
import {
  api,
  assetUrl,
  SLOT_ACTION_LABEL,
  VERSION_MODE_LABEL,
  type FaceSwapStatus,
  type Slot,
  type SlotAction,
  type VersionDetail,
} from "../api";
import { CompareView } from "./CompareView";
import { FaceScore } from "./FaceScore";
import { useConfirm, useToast } from "./feedback";
import { useGenerateDialog } from "./GenerateDialog";

const ACTION_HINT: Record<SlotAction, string> = {
  same: "沿用原来的参考照片、动作和提示词，再生成一次。",
  identity_boost: "用当前照片重新生成，并加强「像本人」的要求：长相优先于风格和动作，画面会更朴素、面部更大更正。",
  change_pose: "保持风格，由 AI 导演写一个这个风格里还没拍过的新动作。",
  change_shot: "保持这张照片的动作和互动，由 AI 导演改写成目标景别后重新生成（用原始照片，不以这张生成图为参考）。",
};

/**
 * Every action that creates tasks or deletes files on slots goes through here,
 * so confirmation, model choice, error toasts and refresh behave the same everywhere.
 */
export function useSlotActions(onChanged: () => Promise<void> | void) {
  const toast = useToast();
  const { ask, dialog: generateDialog } = useGenerateDialog();
  const { confirm, dialog: confirmDialog } = useConfirm();
  const [busy, setBusy] = useState(false);

  const run = async (slots: Slot[], action: SlotAction) => {
    if (slots.length === 0) return false;
    const label = SLOT_ACTION_LABEL[action];
    const choice = await ask({
      title: slots.length === 1 ? label : `为 ${slots.length} 张作品${label}`,
      message: `${ACTION_HINT[action]} 原来的版本会保留在历史版本中。`,
      taskCount: slots.length,
      // Multi-select may mix shot sizes, so no single "current" is excluded there.
      shotChoice: action === "change_shot" ? { current: slots.length === 1 ? slots[0].shot_size : null } : undefined,
    });
    if (!choice) return false;
    setBusy(true);
    try {
      if (slots.length > 1) await api.batchAction(slots.map((s) => s.id), action, choice.model, choice.shot);
      else if (action === "change_pose") await api.changePose(slots[0].id, choice.model);
      else if (action === "change_shot") await api.changeShot(slots[0].id, choice.shot!, choice.model);
      else await api.regenerateSlot(slots[0].id, action, choice.model);
      toast.info(`已提交 ${slots.length} 个生成任务`);
      await onChanged();
      return true;
    } catch (err) {
      toast.error(err);
      return false;
    } finally {
      setBusy(false);
    }
  };

  // Local and free, and the original stays in history: no model / cost dialog (PRD 4.5).
  const faceSwap = async (slots: Slot[]) => {
    if (slots.length === 0) return false;
    setBusy(true);
    try {
      if (slots.length === 1) await api.faceSwap(slots[0].id);
      else await api.batchFaceSwap(slots.map((s) => s.id));
      toast.info(`已开始换脸修正 ${slots.length} 张，本地处理、不产生费用，原图保留在历史版本中`);
      await onChanged();
      return true;
    } catch (err) {
      toast.error(err);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const remove = async (slots: Slot[]) => {
    if (slots.length === 0) return false;
    const withHistory = slots.filter((s) => s.version_count > 1).length;
    const ok = await confirm({
      title: slots.length === 1 ? "删除这张作品？" : `删除 ${slots.length} 张作品？`,
      message: `作品及其全部历史版本的图片都会被删除，无法恢复。${
        withHistory > 0 ? `其中 ${withHistory} 张有多个历史版本，也会一并删除。` : ""
      }`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return false;
    setBusy(true);
    let deleted = 0;
    try {
      for (const slot of slots) {
        await api.deleteSlot(slot.id);
        deleted += 1;
      }
      toast.info(`已删除 ${deleted} 张作品`);
      return true;
    } catch (err) {
      toast.error(err);
      return false;
    } finally {
      setBusy(false);
      await onChanged();
    }
  };

  return { run, faceSwap, remove, busy, dialogs: (
    <>
      {generateDialog}
      {confirmDialog}
    </>
  ) };
}

// ---------- FaceFusion availability ----------

let faceSwapStatus: Promise<FaceSwapStatus> | null = null;

/** Read once per page load; availability only changes when the backend restarts. */
export function useFaceSwapStatus() {
  const [status, setStatus] = useState<FaceSwapStatus | null>(null);
  useEffect(() => {
    faceSwapStatus ??= api.health().then((h) => h.face_swap);
    let alive = true;
    faceSwapStatus
      .then((s) => alive && setStatus(s))
      .catch(() => {
        faceSwapStatus = null;
        if (alive) setStatus({ available: false, mode: null, reason: "无法连接本地服务，请确认后端已启动" });
      });
    return () => {
      alive = false;
    };
  }, []);
  return status;
}

// ---------- "⋯" menu ----------

export interface MenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** Tooltip, e.g. why the item is disabled. */
  title?: string;
}

export function SlotMenu({ items }: { items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div className="menu" ref={ref}>
      <button
        className="menu-trigger"
        aria-label="更多操作"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ⋯
      </button>
      {open && (
        <div className="menu-list card">
          {items.map((item) => (
            <button
              key={item.label}
              className={`menu-item ${item.danger ? "menu-item-danger" : ""}`}
              disabled={item.disabled}
              title={item.title}
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export const download = (assetId: string) => {
  const a = document.createElement("a");
  a.href = assetUrl(assetId);
  a.download = "";
  a.click();
};

// ---------- Version comparison ----------

/** A face swap keeps its source's composition, and so do two swaps of the same source. */
const sameComposition = (a: VersionDetail, b: VersionDetail) =>
  a.source_version_id === b.id ||
  b.source_version_id === a.id ||
  (a.source_version_id !== null && a.source_version_id === b.source_version_id);

const versionTitle = (v: VersionDetail) =>
  `V${v.version_number} · ${v.regenerate_mode ? VERSION_MODE_LABEL[v.regenerate_mode] : "首次生成"}`;

function VersionPairCompare({
  slot,
  versions,
  onClose,
  onRestored,
}: {
  slot: Slot;
  versions: [VersionDetail, VersionDetail];
  onClose: () => void;
  onRestored: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [restoring, setRestoring] = useState(false);

  const restore = async (v: VersionDetail) => {
    setRestoring(true);
    try {
      await api.restoreVersion(slot.id, v.id);
      toast.info(`已将 V${v.version_number} 设为当前版本`);
      await onRestored();
      onClose();
    } catch (err) {
      toast.error(err);
    } finally {
      setRestoring(false);
    }
  };

  // Older version first, so "A → B" reads as "before → after".
  const ordered = [...versions].sort((a, b) => a.version_number - b.version_number);

  return (
    <CompareView
      title={`对比 · ${slot.pose_name ?? slot.style_name}`}
      aligned={sameComposition(versions[0], versions[1])}
      onClose={onClose}
      items={ordered.map((v) => ({
        key: v.id,
        assetId: v.asset_id,
        title: versionTitle(v),
        subtitle: v.is_current ? "当前版本" : (v.model ?? undefined),
        scores: v.face_scores,
        actions: [
          {
            label: v.is_current ? "当前版本" : "设为当前",
            primary: !v.is_current,
            disabled: v.is_current || restoring,
            onSelect: () => restore(v),
          },
          { label: "下载", onSelect: () => download(v.asset_id) },
        ],
      }))}
    />
  );
}

/** "对比原图": the current face-swap version against the version it was swapped from. */
export function SourceCompare({
  slot,
  onClose,
  onChanged,
}: {
  slot: Slot;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [pair, setPair] = useState<[VersionDetail, VersionDetail] | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .versions(slot.id)
      .then((list) => {
        if (!alive) return;
        const current = list.find((v) => v.is_current);
        const source = current && list.find((v) => v.id === current.source_version_id);
        if (current && source) setPair([source, current]);
        else {
          toast.info("原图版本已不存在，无法对比");
          onClose();
        }
      })
      .catch((err) => {
        toast.error(err);
        onClose();
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slot.id]);

  if (!pair) return null;
  return <VersionPairCompare slot={slot} versions={pair} onClose={onClose} onRestored={onChanged} />;
}

// ---------- History versions ----------

export function VersionsDialog({
  slot,
  onClose,
  onChanged,
}: {
  slot: Slot;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [versions, setVersions] = useState<VersionDetail[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [comparing, setComparing] = useState(false);

  const load = () =>
    api
      .versions(slot.id)
      .then(setVersions)
      .catch(() => setFailed(true));

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slot.id]);

  const restore = async (version: VersionDetail) => {
    setRestoring(version.id);
    try {
      await api.restoreVersion(slot.id, version.id);
      toast.info(`已恢复为 V${version.version_number}`);
      await Promise.all([load(), onChanged()]);
    } catch (err) {
      toast.error(err);
    } finally {
      setRestoring(null);
    }
  };

  // Keep at most two picks; a third replaces the oldest pick.
  const togglePick = (id: string) =>
    setPicked((prev) => (prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id].slice(-2)));

  const pickedVersions = (versions ?? []).filter((v) => picked.includes(v.id));
  const byId = new Map((versions ?? []).map((v) => [v.id, v]));

  if (comparing && pickedVersions.length === 2) {
    return (
      <VersionPairCompare
        slot={slot}
        versions={[pickedVersions[0], pickedVersions[1]]}
        onClose={() => setComparing(false)}
        onRestored={async () => {
          await Promise.all([load(), onChanged()]);
        }}
      />
    );
  }

  return (
    <div className="backdrop" onClick={onClose}>
      <div className="card modal versions-modal" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-header" style={{ padding: 0, border: "none", gap: 12 }}>
          <h2>历史版本 · {slot.pose_name ?? slot.style_name}</h2>
          <span className="selection-spacer" />
          {versions && versions.length > 1 && (
            <span className="field-hint">{picked.length === 2 ? "已选 2 个版本" : "勾选两个版本可以对比"}</span>
          )}
          <button className="btn btn-sm btn-primary" disabled={picked.length !== 2} onClick={() => setComparing(true)}>
            对比所选
          </button>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
        </div>
        {failed ? (
          <span className="error-text">无法读取历史版本</span>
        ) : !versions ? (
          <span className="muted">加载中…</span>
        ) : (
          <div className="versions-grid">
            {versions.map((v) => {
              const source = v.source_version_id ? byId.get(v.source_version_id) : undefined;
              const isPicked = picked.includes(v.id);
              return (
                <div key={v.id} className={`version-card ${v.is_current ? "current" : ""}`}>
                  <div className="photo-frame" onClick={() => togglePick(v.id)} style={{ cursor: "pointer" }}>
                    <img src={assetUrl(v.asset_id)} alt={`V${v.version_number}`} style={{ cursor: "pointer" }} />
                    <span className={`checkbox ${isPicked ? "checked" : ""}`}>{isPicked ? "✓" : ""}</span>
                    {v.is_current && <span className="status-chip selected-chip">当前</span>}
                  </div>
                  <div className="version-meta">
                    <span>{versionTitle(v)}</span>
                    {v.source_version_id && (
                      <span className="muted">{source ? `来自 V${source.version_number}` : "来源版本已删除"}</span>
                    )}
                    {v.pose_name && <span className="muted">{v.pose_name}</span>}
                    {v.model && <span className="muted">{v.model}</span>}
                    <FaceScore scores={v.face_scores} />
                    <span className="muted">{new Date(v.created_at).toLocaleString("zh-CN")}</span>
                  </div>
                  <div className="version-actions">
                    <a className="btn btn-sm" href={assetUrl(v.asset_id)} download>
                      下载
                    </a>
                    {!v.is_current && (
                      <button
                        className="btn btn-sm btn-primary"
                        onClick={() => restore(v)}
                        disabled={restoring !== null}
                      >
                        设为当前
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

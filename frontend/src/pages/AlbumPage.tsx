import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, assetUrl, hasFaces, isBroken, isFaceSwapped, isGenerating, SHOT_LABEL, type Slot, type SlotAction } from "../api";
import { AttemptNotice } from "../components/AttemptNotice";
import { CompareView } from "../components/CompareView";
import { EmptyState, useToast } from "../components/feedback";
import { useCurrentProject } from "../components/Layout";
import {
  download,
  SlotMenu,
  SourceCompare,
  useFaceSwapStatus,
  useSlotActions,
  VersionsDialog,
  type MenuItem,
} from "../components/slotActions";

const MAX_COMPARE = 4;

const ALL = "all";

// A slot can take a new paid attempt only when nothing is queued or running for it.
const isIdle = (slot: Slot) => !isGenerating(slot) && slot.latest_task?.status !== "held";

export function AlbumPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const { project, refresh } = useCurrentProject();
  const [params, setParams] = useSearchParams();
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [versionsFor, setVersionsFor] = useState<Slot | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [exporting, setExporting] = useState(false);
  const [sourceCompare, setSourceCompare] = useState<Slot | null>(null);
  const [comparing, setComparing] = useState(false);
  const actions = useSlotActions(refresh);
  const faceSwap = useFaceSwapStatus();

  const photos = project?.photos ?? [];
  const sets = project?.photo_sets ?? [];
  const requested = params.get("style") ?? ALL;
  const active = requested === ALL || sets.some((s) => s.style_id === requested) ? requested : ALL;
  const visible = useMemo(
    () => (active === ALL ? photos : photos.filter((p) => p.style_id === active)),
    [photos, active],
  );

  // Drop selections that disappeared (deleted) or are filtered out.
  useEffect(() => {
    const ids = new Set(visible.map((p) => p.id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [visible]);

  if (!project) return <div className="page muted">加载中…</div>;

  const total = photos.length;
  const done = photos.filter((p) => p.current_version).length;
  const viewable = visible.filter((p) => p.current_version);
  const chosen = visible.filter((p) => selected.has(p.id));
  const chosenIdle = chosen.filter((p) => p.current_version && isIdle(p));
  const chosenViewable = chosen.filter((p) => p.current_version);
  const swapUnavailable = faceSwap && !faceSwap.available ? (faceSwap.reason ?? "换脸修正不可用") : undefined;

  const selectTab = (style: string) => setParams(style === ALL ? {} : { style }, { replace: true });

  const toggle = (slot: Slot) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(slot.id)) next.delete(slot.id);
      else next.add(slot.id);
      return next;
    });

  const exitSelecting = () => {
    setSelecting(false);
    setSelected(new Set());
  };

  const retry = async (slot: Slot) => {
    if (!slot.latest_task) return;
    setRetrying(slot.id);
    try {
      await api.retryTask(slot.latest_task.id);
      await refresh();
    } catch (err) {
      toast.error(err);
    } finally {
      setRetrying(null);
    }
  };

  const exportZip = async (slots?: Slot[]) => {
    setExporting(true);
    try {
      await api.exportZip(project.id, slots?.map((s) => s.id));
    } catch (err) {
      toast.error(err);
    } finally {
      setExporting(false);
    }
  };

  /** Detail shots have no faces, so face-only actions skip them instead of failing the whole batch. */
  const withoutDetailShots = (slots: Slot[]) => {
    const kept = slots.filter(hasFaces);
    if (kept.length < slots.length) toast.info(`已跳过 ${slots.length - kept.length} 张局部特写，画面里没有人脸`);
    return kept;
  };

  const runOnSelection = async (action: SlotAction) => {
    if (chosenIdle.length < chosen.length) {
      toast.info(`已跳过 ${chosen.length - chosenIdle.length} 张正在生成或尚未完成的作品`);
    }
    const slots = action === "identity_boost" ? withoutDetailShots(chosenIdle) : chosenIdle;
    if (slots.length > 0 && (await actions.run(slots, action))) exitSelecting();
  };

  const faceSwapSelection = async () => {
    if (chosenIdle.length < chosen.length) {
      toast.info(`已跳过 ${chosen.length - chosenIdle.length} 张正在处理或尚未完成的作品`);
    }
    const slots = withoutDetailShots(chosenIdle);
    if (slots.length > 0 && (await actions.faceSwap(slots))) exitSelecting();
  };

  const menuFor = (slot: Slot): MenuItem[] => {
    const idle = isIdle(slot) && Boolean(slot.current_version);
    return [
      { label: "重新生成", onSelect: () => actions.run([slot], "same"), disabled: !idle },
      {
        label: "不像本人？加强长相重试",
        onSelect: () => actions.run([slot], "identity_boost"),
        disabled: !idle || !hasFaces(slot),
        title: hasFaces(slot) ? undefined : "局部特写没有人脸，无需加强长相",
      },
      { label: "换动作", onSelect: () => actions.run([slot], "change_pose"), disabled: !idle },
      { label: "换景别…", onSelect: () => actions.run([slot], "change_shot"), disabled: !idle },
      {
        label: "换脸修正",
        onSelect: () => actions.faceSwap([slot]),
        disabled: !idle || !faceSwap?.available || !hasFaces(slot),
        title: !hasFaces(slot)
          ? "局部特写没有人脸，无需换脸"
          : (swapUnavailable ?? "用上传的原照替换两人的脸，本地处理、不产生费用"),
      },
      ...(isFaceSwapped(slot) ? [{ label: "对比原图", onSelect: () => setSourceCompare(slot) }] : []),
      { label: `历史版本（${slot.version_count}）`, onSelect: () => setVersionsFor(slot), disabled: slot.version_count === 0 },
      {
        label: "下载",
        onSelect: () => slot.current_version && download(slot.current_version.asset_id),
        disabled: !slot.current_version,
      },
      { label: "删除", onSelect: () => actions.remove([slot]), danger: true, disabled: isGenerating(slot) },
    ];
  };

  return (
    <div className={`page ${selecting ? "page-with-bar" : ""}`}>
      <div className="page-header">
        <div>
          <h1>婚纱相册</h1>
          <p>{sets.length > 0 ? `${sets.length} 组风格 · 已完成 ${done} / ${total} 张` : "还没有拍组"}</p>
        </div>
        {sets.length > 0 && (
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={() => (selecting ? exitSelecting() : setSelecting(true))}>
              {selecting ? "退出多选" : "多选"}
            </button>
            <button className="btn" onClick={() => exportZip()} disabled={exporting || done === 0}>
              {exporting ? "正在打包…" : "下载全部"}
            </button>
            <button className="btn btn-primary" onClick={() => navigate(`/projects/${project.id}/workspace`)}>
              ＋ 拍新的一组
            </button>
          </div>
        )}
      </div>

      {sets.length === 0 ? (
        <EmptyState
          icon="▦"
          title="相册还没有照片"
          description="在工作台试拍风格后，点击「用这个风格拍一组」，生成成功的照片会立即出现在这里。"
          action={
            <button className="btn btn-primary" onClick={() => navigate(`/projects/${project.id}/workspace`)}>
              前往工作台
            </button>
          }
        />
      ) : (
        <>
          <div className="tabs">
            <button className={`tab ${active === ALL ? "active" : ""}`} onClick={() => selectTab(ALL)}>
              全部 <span className="tab-count">{total}</span>
            </button>
            {sets.map((s) => (
              <button
                key={s.style_id}
                className={`tab ${active === s.style_id ? "active" : ""}`}
                onClick={() => selectTab(s.style_id)}
              >
                {s.style_name}{" "}
                <span className="tab-count">
                  {s.completed_count < s.slot_count ? `${s.completed_count}/${s.slot_count}` : s.slot_count}
                </span>
              </button>
            ))}
          </div>

          <div className="album-grid">
            {visible.map((slot, index) => (
              <PhotoCard
                key={slot.id}
                slot={slot}
                number={index + 1}
                showStyle={active === ALL}
                selecting={selecting}
                selected={selected.has(slot.id)}
                retrying={retrying === slot.id}
                menu={menuFor(slot)}
                onToggle={() => toggle(slot)}
                onView={() => setViewingId(slot.id)}
                onRetry={() => retry(slot)}
                onCompareSource={() => setSourceCompare(slot)}
              />
            ))}
          </div>
        </>
      )}

      {selecting && (
        <div className="selection-bar card">
          <span>已选择 {chosen.length} 张</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set(visible.map((p) => p.id)))}>
            全选
          </button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setSelected(new Set(visible.filter((p) => p.current_version).map((p) => p.id)))}
          >
            仅选已完成
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setSelected(new Set())} disabled={chosen.length === 0}>
            取消全选
          </button>
          <span className="selection-spacer" />
          <button className="btn btn-sm" onClick={() => runOnSelection("same")} disabled={chosenIdle.length === 0 || actions.busy}>
            批量重生
          </button>
          <button
            className="btn btn-sm"
            onClick={() => runOnSelection("identity_boost")}
            disabled={chosenIdle.length === 0 || actions.busy}
          >
            加强长相
          </button>
          <button
            className="btn btn-sm"
            onClick={() => runOnSelection("change_pose")}
            disabled={chosenIdle.length === 0 || actions.busy}
          >
            批量换动作
          </button>
          <button
            className="btn btn-sm"
            onClick={() => runOnSelection("change_shot")}
            disabled={chosenIdle.length === 0 || actions.busy}
          >
            批量换景别
          </button>
          <button
            className="btn btn-sm"
            onClick={faceSwapSelection}
            disabled={chosenIdle.length === 0 || actions.busy || !faceSwap?.available}
            title={swapUnavailable ?? "本地处理、不产生费用"}
          >
            批量换脸修正
          </button>
          <button
            className="btn btn-sm"
            onClick={() => setComparing(true)}
            disabled={chosenViewable.length < 2 || chosenViewable.length > MAX_COMPARE}
            title={`选择 2～${MAX_COMPARE} 张已完成的作品`}
          >
            对比所选
          </button>
          <button
            className="btn btn-sm"
            onClick={() => exportZip(chosen.filter((p) => p.current_version))}
            disabled={exporting || !chosen.some((p) => p.current_version)}
          >
            下载所选
          </button>
          <button
            className="btn btn-sm btn-danger"
            onClick={async () => {
              if (await actions.remove(chosen.filter((p) => !isGenerating(p)))) exitSelecting();
            }}
            disabled={chosen.length === 0 || actions.busy}
          >
            删除所选
          </button>
        </div>
      )}

      {viewingId && (
        <PhotoViewer
          photos={viewable}
          currentId={viewingId}
          onNavigate={setViewingId}
          onClose={() => setViewingId(null)}
          menuFor={menuFor}
        />
      )}
      {versionsFor && (
        <VersionsDialog slot={versionsFor} onClose={() => setVersionsFor(null)} onChanged={refresh} />
      )}
      {sourceCompare && (
        <SourceCompare slot={sourceCompare} onClose={() => setSourceCompare(null)} onChanged={refresh} />
      )}
      {comparing && chosenViewable.length >= 2 && (
        <CompareView
          title={`对比 ${chosenViewable.length} 张作品`}
          aligned={false}
          onClose={() => setComparing(false)}
          items={chosenViewable.slice(0, MAX_COMPARE).map((slot) => ({
            key: slot.id,
            assetId: slot.current_version!.asset_id,
            title: `${String(visible.indexOf(slot) + 1).padStart(2, "0")} ${slot.pose_name ?? slot.style_name}`,
            subtitle: [slot.shot_size ? SHOT_LABEL[slot.shot_size] : null, active === ALL ? slot.style_name : null]
              .filter(Boolean)
              .join(" · "),
            actions: [
              { label: "下载", onSelect: () => download(slot.current_version!.asset_id) },
              {
                label: "历史版本",
                onSelect: () => {
                  setComparing(false);
                  setVersionsFor(slot);
                },
              },
              {
                label: "删除",
                disabled: isGenerating(slot) || actions.busy,
                onSelect: async () => {
                  // The confirm dialog renders under the compare overlay, so close it first.
                  setComparing(false);
                  await actions.remove([slot]);
                },
              },
            ],
          }))}
        />
      )}
      {actions.dialogs}
    </div>
  );
}

function PhotoCard({
  slot,
  number,
  showStyle,
  selecting,
  selected,
  retrying,
  menu,
  onToggle,
  onView,
  onRetry,
  onCompareSource,
}: {
  slot: Slot;
  number: number;
  showStyle: boolean;
  selecting: boolean;
  selected: boolean;
  retrying: boolean;
  menu: MenuItem[];
  onToggle: () => void;
  onView: () => void;
  onRetry: () => void;
  onCompareSource: () => void;
}) {
  const version = slot.current_version;
  const generating = isGenerating(slot);
  const broken = isBroken(slot);

  return (
    <div className={`card photo-card ${selected ? "photo-card-selected" : ""}`}>
      <div className="photo-frame" onClick={selecting ? onToggle : undefined}>
        {version ? (
          <img
            src={assetUrl(version.asset_id)}
            alt={slot.pose_name ?? ""}
            onClick={selecting ? undefined : onView}
            loading="lazy"
          />
        ) : generating ? (
          <div className="shimmer" />
        ) : null}

        {!version && (
          <div className="overlay">
            {generating ? (
              <>
                <div className="spinner" />
                <span>{slot.latest_task?.status === "running" ? "正在生成" : "排队中"}</span>
              </>
            ) : broken ? (
              <>
                <div className="error-text">{slot.latest_task?.error_message ?? "生成失败"}</div>
                {!selecting && (
                  <button className="btn btn-sm" onClick={onRetry} disabled={retrying}>
                    重试
                  </button>
                )}
              </>
            ) : slot.latest_task?.status === "held" ? (
              <span>已搁置，可在任务中心继续</span>
            ) : slot.latest_task?.status === "cancelled" ? (
              <span>已取消</span>
            ) : (
              <span>等待生成</span>
            )}
          </div>
        )}

        {selecting ? (
          <span className={`checkbox ${selected ? "checked" : ""}`}>{selected ? "✓" : ""}</span>
        ) : (
          <span className="status-chip">{String(number).padStart(2, "0")}</span>
        )}
        {version && slot.is_stale && <span className="status-chip stale-chip">旧照片生成</span>}
        {version && isFaceSwapped(slot) && !selecting && (
          <button className="status-chip swap-chip" onClick={onCompareSource} title="与换脸前的原图对比">
            换脸修正 · 对比
          </button>
        )}
      </div>
      <AttemptNotice slot={slot} busy={retrying} onRetry={onRetry} />
      <div className="photo-card-body">
        <div className="photo-card-text">
          <span className="photo-card-pose">{slot.pose_name ?? "—"}</span>
          <span className="muted">
            {[
              slot.shot_size ? SHOT_LABEL[slot.shot_size] : null,
              showStyle ? slot.style_name : null,
              slot.version_count > 1 ? `V${version?.version_number}` : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </div>
        {!selecting && <SlotMenu items={menu} />}
      </div>
    </div>
  );
}

function PhotoViewer({
  photos,
  currentId,
  onNavigate,
  onClose,
  menuFor,
}: {
  photos: Slot[];
  currentId: string;
  onNavigate: (id: string) => void;
  onClose: () => void;
  menuFor: (slot: Slot) => MenuItem[];
}) {
  const index = photos.findIndex((p) => p.id === currentId);
  const slot = photos[index];
  const prev = index > 0 ? photos[index - 1] : null;
  const next = index < photos.length - 1 ? photos[index + 1] : null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && prev) onNavigate(prev.id);
      if (e.key === "ArrowRight" && next) onNavigate(next.id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prev, next, onNavigate, onClose]);

  if (!slot?.current_version) return null;
  const version = slot.current_version;
  // In the viewer, actions close the lightbox so the confirm dialog is not hidden behind it.
  const items = menuFor(slot).map((item) => ({
    ...item,
    onSelect: () => {
      onClose();
      item.onSelect();
    },
  }));

  return (
    <div className="backdrop lightbox" onClick={onClose}>
      <button className="lightbox-close" onClick={onClose} aria-label="关闭">
        ×
      </button>
      {prev && (
        <button
          className="lightbox-nav lightbox-prev"
          onClick={(e) => {
            e.stopPropagation();
            onNavigate(prev.id);
          }}
          aria-label="上一张"
        >
          ‹
        </button>
      )}
      <div className="lightbox-inner" onClick={(e) => e.stopPropagation()}>
        <img src={assetUrl(version.asset_id)} alt={slot.pose_name ?? ""} />
        <div className="lightbox-bar">
          <span className="lightbox-caption">
            {[slot.style_name, slot.pose_name, `V${version.version_number}`, version.model].filter(Boolean).join(" · ")}
          </span>
          {items
            .filter((item) => !item.danger && item.label !== "下载")
            .map((item) => (
              <button key={item.label} className="btn btn-sm" onClick={item.onSelect} disabled={item.disabled}>
                {item.label.startsWith("不像本人") ? "加强长相重试" : item.label}
              </button>
            ))}
          <a className="btn btn-sm" href={assetUrl(version.asset_id)} download>
            下载
          </a>
        </div>
      </div>
      {next && (
        <button
          className="lightbox-nav lightbox-next"
          onClick={(e) => {
            e.stopPropagation();
            onNavigate(next.id);
          }}
          aria-label="下一张"
        >
          ›
        </button>
      )}
    </div>
  );
}

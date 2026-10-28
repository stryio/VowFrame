import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  api,
  assetUrl,
  isBroken,
  isFaceSwapped,
  isGenerating,
  type Person,
  type ProjectDetail,
  type Role,
  type Slot,
  type Style,
} from "../api";
import { AttemptNotice } from "../components/AttemptNotice";
import { CompareView } from "../components/CompareView";
import { EmptyState, Lightbox, useToast } from "../components/feedback";
import { useGenerateDialog } from "../components/GenerateDialog";
import { useCurrentProject } from "../components/Layout";
import { ShootPlanner } from "../components/ShootPlanner";
import {
  download,
  SlotMenu,
  SourceCompare,
  useFaceSwapStatus,
  useSlotActions,
  VersionsDialog,
  type MenuItem,
} from "../components/slotActions";
import { usePolledResource } from "../hooks";

const ROLE_LABEL: Record<Role, string> = { bride: "新娘", groom: "新郎" };

type Step = "upload" | "style";

// Photos present and previews exist: start on the style step.
const initialStep = (p: ProjectDetail): Step =>
  p.stage === "upload" || p.previews.length === 0 ? "upload" : "style";

export function WorkspacePage() {
  const { project } = useCurrentProject();
  // Derived once per project; afterwards only explicit user actions move the step,
  // so background updates (e.g. replacing a photo) never yank the user elsewhere.
  const [step, setStep] = useState<Step | null>(null);
  const initializedFor = useRef<string | null>(null);

  useEffect(() => {
    if (project && initializedFor.current !== project.id) {
      initializedFor.current = project.id;
      setStep(initialStep(project));
    }
  }, [project]);

  if (!project || !step) return <div className="page muted">加载中…</div>;

  const canOpenStyle = project.previews.length > 0;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>婚纱工作台</h1>
          <p>先试拍几种风格，喜欢哪种就用它拍一组，可以拍多组。</p>
        </div>
        <div className="stepper">
          <button className={`step ${step === "upload" ? "active" : "done"}`} onClick={() => setStep("upload")}>
            <span className="step-num">01</span>
            上传照片
          </button>
          <span className="step-line" />
          <button
            className={`step ${step === "style" ? "active" : ""} ${canOpenStyle && step !== "style" ? "done" : ""}`}
            disabled={!canOpenStyle}
            title={canOpenStyle ? undefined : "请先生成风格样片"}
            onClick={() => setStep("style")}
          >
            <span className="step-num">02</span>
            试拍风格
          </button>
        </div>
      </div>

      <RecoveryBanner />

      {step === "upload" && <UploadStep project={project} onNext={() => setStep("style")} />}
      {step === "style" && <StyleStep project={project} onBack={() => setStep("upload")} />}
    </div>
  );
}

// ---------- Recovery banner (PRD 5.3) ----------

function RecoveryBanner() {
  const { project, openTasks } = useCurrentProject();
  const [dismissed, setDismissed] = useState(false);
  if (!project?.has_pending_recovery || dismissed) return null;

  return (
    <div className="banner">
      <div>
        <h3>有待恢复的任务</h3>
        <p className="muted">
          上次有任务没有完成。尚未发出的任务需要你确认才会继续；结果无法确认的任务可以选择重试或放弃。
        </p>
      </div>
      <div className="banner-actions">
        <button className="btn btn-ghost btn-sm" onClick={() => setDismissed(true)}>
          暂不处理
        </button>
        <button className="btn btn-gold btn-sm" onClick={openTasks}>
          去处理
        </button>
      </div>
    </div>
  );
}

// ---------- Step 1: upload ----------

function UploadStep({ project, onNext }: { project: ProjectDetail; onNext: () => void }) {
  const { refresh } = useCurrentProject();
  const toast = useToast();
  const { ask, dialog } = useGenerateDialog();
  const styles = usePolledResource(api.styles, []);
  const [starting, setStarting] = useState(false);
  const ready = Boolean(project.person_a.asset_id && project.person_b.asset_id);
  const hasPreviews = project.previews.length > 0;

  const swapRoles = async () => {
    try {
      await api.updateProject(project.id, {
        person_a_role: project.person_a.role === "bride" ? "groom" : "bride",
      });
      await refresh();
    } catch (err) {
      toast.error(err);
    }
  };

  const start = async () => {
    if (hasPreviews) {
      // Previews already exist: do not silently spend money again.
      onNext();
      return;
    }
    const styleCount = styles.data!.length;
    const choice = await ask({
      title: "开始生成风格样片",
      message: `将为 ${styleCount} 种风格各生成 1 张样片。`,
      taskCount: styleCount,
    });
    if (!choice) return;
    setStarting(true);
    try {
      await api.createPreviews(project.id, { model: choice.model });
      await refresh();
      onNext();
    } catch (err) {
      toast.error(err);
    } finally {
      setStarting(false);
    }
  };

  return (
    <>
      <div className="upload-grid">
        <PersonUpload projectId={project.id} which="a" person={project.person_a} onSwap={swapRoles} />
        <PersonUpload projectId={project.id} which="b" person={project.person_b} onSwap={swapRoles} />
      </div>

      <div className="card tips">
        <h3>照片建议</h3>
        <ul>
          <li>近期照片，五官清晰，没有墨镜、口罩或大面积刘海遮挡</li>
          <li>正脸或接近正脸，自然光</li>
          <li>半身照效果优于证件照；证件照可以用，但效果可能较弱</li>
          <li>每人只需 1 张，多张反而容易让模型混淆长相</li>
        </ul>
      </div>

      <div className="action-bar">
        {!ready && <span className="muted">请先上传两个人的照片</span>}
        <button className="btn btn-primary btn-lg" disabled={!ready || starting || !styles.data} onClick={start}>
          {hasPreviews ? "查看风格样片 →" : starting ? "正在提交…" : "开始生成风格样片"}
        </button>
      </div>
      {dialog}
    </>
  );
}

function PersonUpload({
  projectId,
  which,
  person,
  onSwap,
}: {
  projectId: string;
  which: "a" | "b";
  person: Person;
  onSwap: () => void;
}) {
  const { project, refresh } = useCurrentProject();
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);

  const upload = async (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast.info("请选择图片文件");
      return;
    }
    const replacing = Boolean(person.asset_id);
    const hadResults = Boolean(project && (project.previews.length > 0 || project.photos.length > 0));
    setUploading(true);
    try {
      await api.uploadImage(projectId, which, file);
      if (replacing && hadResults) {
        toast.info("照片已更换，之前的样片和作品都会保留；建议重新生成风格样片");
      }
      await refresh();
    } catch (err) {
      toast.error(err);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="card upload-card">
      <div className="upload-card-header">
        <h3>人物 {which.toUpperCase()}</h3>
        <span className="role-pill">
          {ROLE_LABEL[person.role]}
          <button
            className="btn btn-ghost btn-sm"
            style={{ height: 22, padding: "0 6px" }}
            onClick={onSwap}
            title="互换两人的角色"
          >
            ⇄ 互换
          </button>
        </span>
      </div>

      <div
        className={`dropzone ${dragging ? "dragging" : ""} ${person.asset_id ? "filled" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          upload(e.dataTransfer.files[0]);
        }}
      >
        {person.asset_id ? (
          <img src={assetUrl(person.asset_id)} alt={ROLE_LABEL[person.role]} />
        ) : (
          <>
            <div className="dropzone-icon">＋</div>
            <div>点击或拖拽上传{ROLE_LABEL[person.role]}的照片</div>
          </>
        )}
        {uploading && (
          <div className="dropzone-busy">
            <div className="spinner" />
          </div>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        hidden
        onChange={(e) => {
          upload(e.target.files?.[0]);
          e.target.value = "";
        }}
      />

      {person.asset_id && (
        <div className="upload-card-actions">
          <button className="btn btn-sm" onClick={() => inputRef.current?.click()}>
            更换照片
          </button>
        </div>
      )}
    </div>
  );
}

// ---------- Step 2: style previews and shooting sets ----------

function StyleStep({ project, onBack }: { project: ProjectDetail; onBack: () => void }) {
  const { refresh } = useCurrentProject();
  const navigate = useNavigate();
  const toast = useToast();
  const { ask, dialog } = useGenerateDialog();
  const styles = usePolledResource(api.styles, []);
  const [viewing, setViewing] = useState<Slot | null>(null);
  const [versionsFor, setVersionsFor] = useState<Slot | null>(null);
  const [planning, setPlanning] = useState<Slot | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [sourceCompare, setSourceCompare] = useState<Slot | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [comparing, setComparing] = useState(false);
  const actions = useSlotActions(refresh);
  const faceSwap = useFaceSwapStatus();

  if (!styles.data) return <div className="muted">加载中…</div>;

  const idle = (slot: Slot) =>
    Boolean(slot.current_version) && !isGenerating(slot) && slot.latest_task?.status !== "held";

  const menuFor = (slot: Slot): MenuItem[] => [
    {
      label: "不像本人？加强长相重试",
      onSelect: () => actions.run([slot], "identity_boost"),
      disabled: !idle(slot),
    },
    {
      label: "换脸修正",
      onSelect: () => actions.faceSwap([slot]),
      disabled: !idle(slot) || !faceSwap?.available,
      title:
        faceSwap && !faceSwap.available
          ? (faceSwap.reason ?? "换脸修正不可用")
          : "用上传的原照替换两人的脸，本地处理、不产生费用",
    },
    ...(isFaceSwapped(slot) ? [{ label: "对比原图", onSelect: () => setSourceCompare(slot) }] : []),
    {
      label: `历史版本（${slot.version_count}）`,
      onSelect: () => setVersionsFor(slot),
      disabled: slot.version_count === 0,
    },
  ];

  const slotByStyle = new Map(project.previews.map((s) => [s.style_id, s]));
  const setByStyle = new Map(project.photo_sets.map((s) => [s.style_id, s]));
  const anyStale = project.previews.some((s) => s.is_stale);
  const allFailed =
    project.previews.length > 0 && project.previews.every((s) => !s.current_version && isBroken(s));

  const regenerate = async (styleIds: string[] | undefined, label: string) => {
    const choice = await ask({
      title: label,
      message: "已有的样片会保留为历史版本。",
      taskCount: styleIds?.length ?? styles.data!.length,
    });
    if (!choice) return;
    setBusy(styleIds?.[0] ?? "all");
    try {
      await api.createPreviews(project.id, { styleIds, model: choice.model });
      await refresh();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(null);
    }
  };

  const retry = async (slot: Slot) => {
    if (!slot.latest_task) return;
    setBusy(slot.style_id);
    try {
      await api.retryTask(slot.latest_task.id);
      await refresh();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(null);
    }
  };

  const shoot = (slot: Slot) => {
    if (slot.is_stale) toast.info("这张样片是用旧照片生成的，建议先重新生成样片再拍组");
    setPlanning(slot);
  };

  const togglePick = (slot: Slot) =>
    setPicked((prev) => (prev.includes(slot.id) ? prev.filter((id) => id !== slot.id) : [...prev, slot.id]));
  const exitSelecting = () => {
    setSelecting(false);
    setPicked([]);
  };
  // Keep the built-in style order rather than click order.
  const pickedSlots = project.previews.filter((s) => picked.includes(s.id) && s.current_version);

  return (
    <>
      <div className="page-header">
        <p className="muted">
          每种风格先生成 1 张样片。喜欢哪种，就点「用这个风格拍一组」；可以拍多种风格，也可以同一风格多拍几组。
        </p>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost" onClick={onBack}>
            ← 修改照片
          </button>
          <button className="btn" onClick={() => (selecting ? exitSelecting() : setSelecting(true))}>
            {selecting ? "退出对比选择" : "对比样片"}
          </button>
          <button
            className="btn"
            onClick={() => regenerate(undefined, "重新生成全部样片")}
            disabled={busy !== null}
          >
            重新生成全部
          </button>
        </div>
      </div>

      {anyStale && (
        <div className="notice">
          照片已更换，标有「旧照片生成」的样片仍是用之前的照片生成的。用它拍组，成片可能更像旧照片里的人，建议先重新生成。
        </div>
      )}

      {allFailed ? (
        <EmptyState
          icon="!"
          title="样片全部生成失败"
          description="可能是网络或模型服务暂时不可用，也可能是照片未通过审核。请检查设置后重试。"
          action={
            <button className="btn btn-primary" onClick={() => regenerate(undefined, "重新生成全部样片")}>
              全部重试
            </button>
          }
        />
      ) : (
        <div className="preview-grid">
          {styles.data.map((style) => (
            <PreviewCard
              key={style.id}
              style={style}
              slot={slotByStyle.get(style.id)}
              shotCount={setByStyle.get(style.id)?.slot_count ?? 0}
              busy={busy === style.id || busy === "all"}
              onView={setViewing}
              onRegenerate={() => regenerate([style.id], `${slotByStyle.get(style.id) ? "重新生成" : "生成"}「${style.name}」样片`)}
              onRetry={retry}
              onShoot={shoot}
              menu={slotByStyle.get(style.id) ? menuFor(slotByStyle.get(style.id)!) : null}
              onOpenAlbum={() => navigate(`/projects/${project.id}/album?style=${style.id}`)}
              selecting={selecting}
              selected={picked.includes(slotByStyle.get(style.id)?.id ?? "")}
              onToggle={togglePick}
              onCompareSource={setSourceCompare}
            />
          ))}
        </div>
      )}

      {viewing?.current_version && (
        <Lightbox
          src={assetUrl(viewing.current_version.asset_id)}
          caption={[
            viewing.style_name,
            `V${viewing.current_version.version_number}`,
            viewing.current_version.model,
          ]
            .filter(Boolean)
            .join(" · ")}
          onClose={() => setViewing(null)}
        />
      )}
      {versionsFor && (
        <VersionsDialog slot={versionsFor} onClose={() => setVersionsFor(null)} onChanged={refresh} />
      )}
      {sourceCompare && (
        <SourceCompare slot={sourceCompare} onClose={() => setSourceCompare(null)} onChanged={refresh} />
      )}
      {/* Room so the fixed bar never covers the last row of cards. */}
      {selecting && <div style={{ height: 64 }} />}
      {selecting && (
        <div className="selection-bar card">
          <span>已选择 {pickedSlots.length} 张样片</span>
          <span className="field-hint">选 2～4 张已生成的样片并排对比</span>
          <span className="selection-spacer" />
          <button className="btn btn-ghost btn-sm" onClick={() => setPicked([])} disabled={picked.length === 0}>
            取消全选
          </button>
          <button
            className="btn btn-sm btn-primary"
            onClick={() => setComparing(true)}
            disabled={pickedSlots.length < 2 || pickedSlots.length > 4}
          >
            对比所选
          </button>
        </div>
      )}
      {comparing && pickedSlots.length >= 2 && (
        <CompareView
          title={`对比 ${pickedSlots.length} 种风格`}
          aligned={false}
          onClose={() => setComparing(false)}
          items={pickedSlots.slice(0, 4).map((slot) => {
            const shot = setByStyle.get(slot.style_id)?.slot_count ?? 0;
            return {
              key: slot.id,
              assetId: slot.current_version!.asset_id,
              title: slot.style_name,
              subtitle: [`V${slot.current_version!.version_number}`, slot.current_version!.model]
                .filter(Boolean)
                .join(" · "),
              actions: [
                {
                  label: shot > 0 ? "再拍一组" : "用这个风格拍一组",
                  primary: true,
                  onSelect: () => {
                    setComparing(false);
                    exitSelecting();
                    shoot(slot);
                  },
                },
                { label: "下载", onSelect: () => download(slot.current_version!.asset_id) },
              ],
            };
          })}
        />
      )}
      {planning && (
        <ShootPlanner
          projectId={project.id}
          preview={planning}
          shotCount={setByStyle.get(planning.style_id)?.slot_count ?? 0}
          onClose={() => setPlanning(null)}
          onShot={async () => {
            const styleId = planning.style_id;
            setPlanning(null);
            await refresh();
            navigate(`/projects/${project.id}/album?style=${styleId}`);
          }}
        />
      )}
      {actions.dialogs}
      {dialog}
    </>
  );
}

function PreviewCard({
  style,
  slot,
  shotCount,
  busy,
  onView,
  onRegenerate,
  onRetry,
  onShoot,
  onOpenAlbum,
  menu,
  selecting,
  selected,
  onToggle,
  onCompareSource,
}: {
  style: Style;
  slot: Slot | undefined;
  shotCount: number;
  busy: boolean;
  onView: (slot: Slot) => void;
  onRegenerate: () => void;
  onRetry: (slot: Slot) => void;
  onShoot: (slot: Slot) => void;
  onOpenAlbum: () => void;
  menu: MenuItem[] | null;
  selecting: boolean;
  selected: boolean;
  onToggle: (slot: Slot) => void;
  onCompareSource: (slot: Slot) => void;
}) {
  const task = slot?.latest_task;
  const version = slot?.current_version;
  const generating = slot ? isGenerating(slot) : false;
  const failed = slot ? isBroken(slot) : false;

  return (
    <div className={`card preview-card ${shotCount > 0 || selected ? "selected" : ""}`}>
      <div className="photo-frame" onClick={selecting && slot && version ? () => onToggle(slot) : undefined}>
        {version ? (
          <img
            src={assetUrl(version.asset_id)}
            alt={style.name}
            onClick={selecting ? undefined : () => slot && onView(slot)}
            style={selecting ? { cursor: "pointer" } : undefined}
          />
        ) : generating ? (
          <div className="shimmer" />
        ) : null}

        {!version && !generating && (
          <div className="overlay">
            {failed ? (
              <>
                <div className="error-text">{task?.error_message ?? "生成失败"}</div>
                <button className="btn btn-sm" onClick={() => slot && onRetry(slot)} disabled={busy}>
                  重试
                </button>
              </>
            ) : task?.status === "held" ? (
              <span>已搁置，等待你在任务中心确认继续</span>
            ) : (
              <span>尚未生成</span>
            )}
          </div>
        )}

        {generating && !version && (
          <div className="overlay">
            <div className="spinner" />
            <span>{task?.status === "running" ? "正在生成" : "排队中"}</span>
          </div>
        )}
        {version && slot?.is_stale && !generating && <span className="status-chip stale-chip">旧照片生成</span>}
        {selecting && version && (
          <span className={`checkbox ${selected ? "checked" : ""}`}>{selected ? "✓" : ""}</span>
        )}
        {version && slot && isFaceSwapped(slot) && !selecting && (
          <button className="status-chip swap-chip" onClick={() => onCompareSource(slot)} title="与换脸前的原图对比">
            换脸修正 · 对比
          </button>
        )}
        {shotCount > 0 && !selecting && (
          <button className="status-chip selected-chip" onClick={onOpenAlbum} title="在相册中查看">
            已拍 {shotCount} 张 →
          </button>
        )}
      </div>

      {slot && <AttemptNotice slot={slot} busy={busy} onRetry={() => onRetry(slot)} />}

      <div className="preview-body">
        <div className="preview-title">
          <h3>{style.name}</h3>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            {version?.model && <span className="tag">{version.model}</span>}
            {version && slot && slot.version_count > 1 && <span className="tag">V{version.version_number}</span>}
            {menu && <SlotMenu items={menu} />}
          </div>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>
          {style.description}
        </p>
        <div className="preview-actions">
          <button className="btn btn-sm" onClick={onRegenerate} disabled={busy || generating}>
            {slot ? "重新生成" : "生成样片"}
          </button>
          <button
            className={`btn btn-sm ${shotCount > 0 ? "btn-gold" : "btn-primary"}`}
            onClick={() => slot && onShoot(slot)}
            disabled={!version || busy}
          >
            {shotCount > 0 ? "再拍一组" : "用这个风格拍一组"}
          </button>
        </div>
      </div>
    </div>
  );
}

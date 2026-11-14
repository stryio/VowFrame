import { Fragment, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  api,
  assetUrl,
  isBroken,
  fixLabel,
  isFaceSwapped,
  isGenerating,
  FACING_NAMES,
  SIDE_FACINGS,
  type BrideHairLength, type Facing, type GroomHairLength, type Look, type LookItem, type LookOptions, type SideFacing,
  type Makeup, type Person, type RetouchSettings,
  type ProjectDetail,
  type Role,
  type Slot,
  type Style,
} from "../api";
import { AttemptNotice } from "../components/AttemptNotice";
import { CompareView } from "../components/CompareView";
import { MAKEUP_PRESETS, MakeupAdjustments, makeupPresetLabel, retouchSummary } from "../components/MakeupAdjustments";
import { FaceScore, faceScoreText } from "../components/FaceScore";
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

type Step = "upload" | "looks" | "style";

/** Some style has both looks selected (V7.0: the bride's for that style and the groom's shared one). */
const hasStyleLooks = (p: ProjectDetail) => Object.values(p.style_looks).some((pair) => pair.bride && pair.groom);

// Samples exist: start on the style step; photos uploaded: on the looks step.
const initialStep = (p: ProjectDetail): Step =>
  p.stage === "upload" ? "upload" : p.previews.length > 0 ? "style" : "looks";

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

  const canOpenLooks = project.stage !== "upload";
  const canOpenStyle = project.previews.length > 0 || hasStyleLooks(project);

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>婚纱工作台</h1>
          <p>选好风格和两人的妆发，再选服饰和场景试拍；看过样片，满意后再拍一组。</p>
        </div>
        <div className="stepper">
          <button className={`step ${step === "upload" ? "active" : "done"}`} onClick={() => setStep("upload")}>
            <span className="step-num">01</span>
            上传照片
          </button>
          <span className="step-line" />
          <button
            className={`step ${step === "looks" ? "active" : ""} ${canOpenStyle && step !== "looks" ? "done" : ""}`}
            disabled={!canOpenLooks}
            title={canOpenLooks ? undefined : "请先上传两个人的照片"}
            onClick={() => setStep("looks")}
          >
            <span className="step-num">02</span>
            风格与造型
          </button>
          <span className="step-line" />
          <button
            className={`step ${step === "style" ? "active" : ""} ${project.previews.length > 0 && step !== "style" ? "done" : ""}`}
            disabled={!canOpenStyle}
            title={canOpenStyle ? undefined : "请先选用新郎的造型和至少一个风格的新娘造型"}
            onClick={() => setStep("style")}
          >
            <span className="step-num">03</span>
            试拍风格
          </button>
        </div>
      </div>

      <RecoveryBanner />

      {step === "upload" && <UploadStep project={project} onNext={() => setStep("looks")} />}
      {step === "looks" && <LooksStep key={project.id} project={project} onBack={() => setStep("upload")} onNext={() => setStep("style")} />}
      {step === "style" && <StyleStep project={project} onBack={() => setStep("looks")} />}
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

const BRIDE_HAIR_OPTIONS: [BrideHairLength, string][] = [
  ["long", "长发"],
  ["medium", "及肩"],
  ["short", "短发"],
];

const GROOM_HAIR_OPTIONS: [GroomHairLength, string][] = [
  ["buzz", "寸头"],
  ["short", "短发"],
  ["medium", "中长发"],
];

type LookField = "makeup_level" | "groom_makeup_level" | "bride_hair_length" | "groom_hair_length";

/** Hair lengths and makeup levels, on the looks step: they decide the hairstyles offered and how a look is made
 * up (V7.0). Either only affects looks generated afterwards. */
function MakeupCard({ project }: { project: ProjectDetail }) {
  const { refresh } = useCurrentProject();
  const toast = useToast();

  const setValue = async (field: LookField, value: string) => {
    try {
      await api.updateProject(project.id, { [field]: value });
      await refresh();
    } catch (err) {
      toast.error(err);
    }
  };

  const row = (field: LookField, label: string, options: [string, string][]) => (
    <div className="makeup-options">
      <span className="makeup-role">{label}</span>
      <div className="shot-switch" role="group" aria-label={label}>
        {options.map(([value, text]) => (
          <button
            key={value}
            className={`shot-pill ${project[field] === value ? "active" : ""}`}
            disabled={project[field] === value}
            onClick={() => setValue(field, value)}
          >
            {text}
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <div className="card tips makeup-card">
      <h3>头发与妆容</h3>
      <div className="makeup-rows">
        {row("bride_hair_length", "新娘头发", BRIDE_HAIR_OPTIONS)}
        {row("groom_hair_length", "新郎头发", GROOM_HAIR_OPTIONS)}
        {row("makeup_level", "新娘妆容", MAKEUP_PRESETS.bride)}
        {row("groom_makeup_level", "新郎精修", MAKEUP_PRESETS.groom)}
        <span className="muted">
          头发长度请按照片里的实际长度选，它决定能做哪些发型，长度保持和照片一致。选择妆容预设会同时重设提亮、遮瑕和轮廓选项；
          可在下面化妆或修脸的「精细调整」中分别修改。新郎只在「精致」档化淡妆；新娘的妆色和妆面按风格配。改了只影响之后新生成的候选图。
        </span>
      </div>
    </div>
  );
}

function UploadStep({ project, onNext }: { project: ProjectDetail; onNext: () => void }) {
  const { refresh } = useCurrentProject();
  const toast = useToast();
  const ready = Boolean(project.person_a.asset_id && project.person_b.asset_id);

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
          <li>正面照必传：正脸或接近正脸，自然光</li>
          <li>半身照效果优于证件照；证件照可以用，但效果可能较弱</li>
          <li>侧面照可选：约 45° 侧脸，只在脸朝同一侧的镜头里使用</li>
          <li>素颜照也可以，发型在下一步「选造型」里做，妆容在拍摄时化；不要用美颜或修过图的照片</li>
          <li>上传后会自动裁到头部和肩膀，缩略图就是实际使用的照片；照片里只能有本人</li>
        </ul>
      </div>

      <div className="action-bar">
        {!ready && <span className="muted">请先上传两个人的照片</span>}
        <button className="btn btn-primary btn-lg" disabled={!ready} onClick={onNext}>
          下一步：选造型 →
        </button>
      </div>
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
  const sideInputRefs = {
    left: useRef<HTMLInputElement>(null),
    right: useRef<HTMLInputElement>(null),
    left_full: useRef<HTMLInputElement>(null),
    right_full: useRef<HTMLInputElement>(null),
  };
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);

  const upload = async (file: File | undefined, angle: Facing = "front") => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast.info("请选择图片文件");
      return;
    }
    // Samples use only the front photos, so only replacing a front photo makes them outdated.
    const replacing = angle === "front" && Boolean(person.asset_id);
    const hadResults = Boolean(project && (project.previews.length > 0 || project.photos.length > 0));
    setUploading(true);
    try {
      await api.uploadImage(projectId, which, file, angle);
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
          upload(e.dataTransfer.files[0], "front");
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
      <div className="side-upload-grid">
        {SIDE_FACINGS.map((angle) => {
          const assetId = person[`${angle}_asset_id`];
          const label = SIDE_UPLOAD_LABELS[angle];
          return (
            <div className="side-upload" key={angle}>
              <div className="side-upload-label">{label}</div>
              <button className={`side-dropzone ${assetId ? "filled" : ""}`} onClick={() => sideInputRefs[angle].current?.click()}>
                {assetId ? <img src={assetUrl(assetId)} alt={label} /> : <span>＋ 上传</span>}
              </button>
              <input ref={sideInputRefs[angle]} type="file" accept="image/*" hidden onChange={(e) => { upload(e.target.files?.[0], angle); e.target.value = ""; }} />
              {assetId && <button className="btn btn-ghost btn-sm" onClick={async () => { try { await api.deleteSideImage(projectId, which, angle); await refresh(); } catch (err) { toast.error(err); } }}>删除</button>}
            </div>
          );
        })}
      </div>
      <p className="side-upload-hint muted">侧脸照可选。传了哪个角度，AI 导演才会设计这个角度、看得清脸的镜头；背影和远景不受限制。</p>
    </div>
  );
}

const SIDE_UPLOAD_LABELS: Record<SideFacing, string> = {
  left: "脸朝画面左 · 约 45°",
  right: "脸朝画面右 · 约 45°",
  left_full: "脸朝画面左 · 正侧面 约 90°",
  right_full: "脸朝画面右 · 正侧面 约 90°",
};

// ---------- Step 2: styles and looks (V5.0; per style since V7.0) ----------

const lookLabel = (look: Look) => (look.jewelry_name ? `${look.hairstyle_name} · ${look.jewelry_name}` : look.hairstyle_name);

// Example pictures of a fictional couple, made once by backend/scripts/make_examples.py.
const hairExample = (role: Role, hairstyleId: string) => `/examples/hair/${role}_${hairstyleId}.webp`;
const styleExample = (styleId: string) => `/examples/styles/${styleId}.webp`;

/** A choice shown with its example picture: the card toggles it, the corner button enlarges the picture. */
function ExampleCard({
  src,
  label,
  on,
  disabled,
  title,
  onToggle,
  onZoom,
}: {
  src: string;
  label: React.ReactNode;
  on: boolean;
  disabled?: boolean;
  title?: string;
  onToggle: () => void;
  onZoom: () => void;
}) {
  const [missing, setMissing] = useState(false);
  return (
    <div className={`example-card ${on ? "on" : ""} ${disabled ? "disabled" : ""}`} title={title}>
      <button className="example-pick" onClick={onToggle} disabled={disabled} aria-pressed={on}>
        <span className="example-photo">
          {missing ? <span className="muted">暂无示例</span> : <img src={src} alt="" loading="lazy" onError={() => setMissing(true)} />}
        </span>
        <span className="example-label">
          <span className={`example-check ${on ? "checked" : ""}`}>{on ? "✓" : ""}</span>
          {label}
        </span>
      </button>
      {!missing && (
        <button className="example-zoom" onClick={onZoom} aria-label="看大图" title="看大图">
          ⤢
        </button>
      )}
    </div>
  );
}

function LooksStep({ project, onBack, onNext }: { project: ProjectDetail; onBack: () => void; onNext: () => void }) {
  const options = usePolledResource(api.lookOptions, []);
  const styles = usePolledResource(api.styles, []);
  // Keep existing work available even if only the makeup step has been completed.
  const [picked, setPicked] = useState<string[]>([]);
  const [active, setActive] = useState("groom");
  const [choosingStyles, setChoosingStyles] = useState(false);
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const stylePickerRef = useRef<HTMLDetailsElement>(null);
  const id = useId();
  const ready = hasStyleLooks(project);
  const bride = project.person_a.role === "bride" ? "a" : "b";
  const groom = bride === "a" ? "b" : "a";

  if (!options.data || !styles.data) return <div className="muted">加载中…</div>;

  const withCandidates = new Set(
    [...project.looks, ...project.makeups].filter((item) => item.person === bride && item.style_id).map((item) => item.style_id),
  );
  const sampled = new Set(project.previews.map((slot) => slot.style_id));
  // A retired style may only refresh a sample this project already has.
  const offered = styles.data.filter((style) => !style.retired || sampled.has(style.id));
  const chosen = offered.filter((style) => withCandidates.has(style.id) || picked.includes(style.id));
  const activeId = active === "groom" || chosen.some((style) => style.id === active) ? active : "groom";
  const readyCount = chosen.filter((style) => project.looks.some(
    (look) => look.person === bride && look.style_id === style.id && look.selected && look.usable,
  )).length;
  const selectTab = (value: string) => {
    setActive(value);
    workspaceRef.current?.scrollIntoView({ block: "start" });
    document.getElementById(`${id}-tab-${value}`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  const openStylePicker = () => {
    setChoosingStyles(true);
    stylePickerRef.current?.scrollIntoView({ block: "start" });
  };
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const tabs = ["groom", ...chosen.map((style) => style.id)];
    const index = tabs.indexOf(activeId);
    let next = index;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    selectTab(tabs[next]);
    document.getElementById(`${id}-tab-${tabs[next]}`)?.focus();
  };
  const renderTab = (value: string, title: string, person: "a" | "b", style?: Style) => {
    const looks = project.looks.filter((look) => look.person === person && look.style_id === (style?.id ?? null));
    const makeups = project.makeups.filter((makeup) => makeup.person === person && makeup.style_id === (style?.id ?? null));
    const selected = looks.find((look) => look.selected && look.usable);
    const busy = [...looks, ...makeups].some((item) => item.status === "waiting" || item.status === "running");
    const failed = [...looks, ...makeups].some((item) => item.status === "failed" || item.status === "interrupted");
    const status = busy ? "生成中" : selected ? "已选用造型" : failed ? "有任务需处理" :
      looks.some((look) => look.usable) ? "待选用造型" : makeups.some((makeup) => makeup.selected && makeup.usable) ? "待做发型" : value === "groom" ? "待修脸" : "待做妆容";
    const tone = busy ? "busy" : selected ? "ready" : failed ? "error" : "pending";
    const thumbnail = selected?.asset_id ? assetUrl(selected.asset_id) : style ? styleExample(style.id) :
      assetUrl((person === "a" ? project.person_a : project.person_b).asset_id!);
    return (
      <button key={value} type="button" role="tab" id={`${id}-tab-${value}`} aria-controls={`${id}-panel-${value}`}
        aria-selected={activeId === value} tabIndex={activeId === value ? 0 : -1}
        className={`looks-nav-item ${activeId === value ? "active" : ""}`} onClick={() => selectTab(value)} onKeyDown={onTabKeyDown}>
        <img src={thumbnail} alt="" loading="lazy" />
        <span className="looks-nav-copy">
          <strong>{title}</strong>
          <span className={`looks-nav-status ${tone}`}>{status}</span>
        </span>
      </button>
    );
  };

  return (
    <>
      <p className="muted looks-intro">
        可多选拍摄风格，每次专心调整一套妆发；从造型列表切换，其他风格的选择会保留。
        每张妆容、修脸照、发型照各是一次付费修图，服饰和场景在下一步选择。
      </p>

      <details className="card looks-disclosure looks-shared-settings">
        <summary>
          <span><strong>共用设置</strong><span className="muted">新娘{BRIDE_HAIR_OPTIONS.find(([value]) => value === project.bride_hair_length)?.[1]} · 新郎{GROOM_HAIR_OPTIONS.find(([value]) => value === project.groom_hair_length)?.[1]} · 妆容预设</span></span>
          <span className="looks-disclosure-toggle" aria-hidden="true" />
        </summary>
        <MakeupCard project={project} />
      </details>

      <details className="card looks-disclosure looks-style-picker" ref={stylePickerRef} open={choosingStyles}
        onToggle={(event) => setChoosingStyles(event.currentTarget.open)}>
        <summary>
          <span><strong>拍摄风格</strong><span className="muted">已选 {chosen.length} 种 · 展开可多选或调整</span></span>
          <span className="looks-disclosure-toggle" aria-hidden="true" />
        </summary>
        <div className="looks-style-picker-body">
          <div className="example-grid style-examples">
            {offered.map((style) => {
              const on = chosen.some((item) => item.id === style.id);
              const locked = withCandidates.has(style.id);
              return (
                <ExampleCard
                  key={style.id}
                  src={styleExample(style.id)}
                  label={style.name}
                  on={on}
                  disabled={locked}
                  title={locked ? "这个风格已经有妆容或造型；删掉全部候选后可以取消" : style.description}
                  onToggle={() => {
                    setPicked((previous) => on ? previous.filter((key) => key !== style.id) : [...previous, style.id]);
                    if (!on) setActive(style.id);
                    else if (activeId === style.id) setActive("groom");
                  }}
                  onZoom={() => setZoom({ src: styleExample(style.id), caption: `${style.name}（示例）：${style.description}` })}
                />
              );
            })}
          </div>
          <div className="looks-style-picker-footer">
            <span className="muted">示例使用虚构模特，仅展示风格。已有候选的风格会保留在列表中。</span>
            <button type="button" className="btn btn-primary btn-sm" onClick={() => { setChoosingStyles(false); selectTab(activeId); }}>
              完成选择，调整造型 →
            </button>
          </div>
        </div>
      </details>
      {zoom && <Lightbox src={zoom.src} caption={zoom.caption} onClose={() => setZoom(null)} />}

      <div className="looks-workspace" ref={workspaceRef}>
        <aside className="card looks-navigation">
          <div className="looks-navigation-heading"><h3>造型列表</h3><span className="muted">{chosen.length ? `新娘 ${readyCount}/${chosen.length} 已就绪` : "尚未选择新娘风格"}</span></div>
          <div role="tablist" aria-label="选择要调整的造型" aria-orientation="vertical" className="looks-nav-list">
            {renderTab("groom", "新郎共用造型", groom)}
            {chosen.map((style) => renderTab(style.id, style.name, bride, style))}
          </div>
          {chosen.length === 0 && <p className="muted">添加拍摄风格后，在这里逐套调整新娘妆发。</p>}
          <button type="button" className="btn btn-sm looks-add-style" onClick={openStylePicker}>＋ 添加 / 调整风格</button>
        </aside>
        <div className="looks-panels">
          {/* Keep panels mounted so switching styles preserves unfinished hair and jewelry choices. */}
          <section role="tabpanel" id={`${id}-panel-groom`} aria-labelledby={`${id}-tab-groom`} hidden={activeId !== "groom"}>
            <PersonLooks project={project} which={groom} role="groom" options={options.data} />
          </section>
          {chosen.map((style) => (
            <section key={style.id} role="tabpanel" id={`${id}-panel-${style.id}`} aria-labelledby={`${id}-tab-${style.id}`} hidden={activeId !== style.id}>
              <PersonLooks project={project} which={bride} role="bride" options={options.data!} style={style} />
            </section>
          ))}
        </div>
      </div>

      <div className="action-bar">
        <button className="btn btn-ghost" onClick={onBack}>
          ← 修改照片
        </button>
        {!ready && <span className="muted">请选用新郎的造型和至少一个风格的新娘造型</span>}
        <button className="btn btn-primary btn-lg" disabled={!ready} onClick={onNext}>
          下一步：选服饰和场景试拍 →
        </button>
      </div>
    </>
  );
}

/** One person's look candidates: the groom's (shared by every style) or the bride's for one style. */
function PersonLooks({
  project,
  which,
  role,
  options,
  style,
}: {
  project: ProjectDetail;
  which: "a" | "b";
  role: Role;
  options: LookOptions;
  style?: Style;
}) {
  const { refresh } = useCurrentProject();
  const toast = useToast();
  const who = ROLE_LABEL[role];
  const length = role === "bride" ? project.bride_hair_length : project.groom_hair_length;
  const needsUpdo = Boolean(style && options.styles[style.id]?.needs_updo);
  const hairstyles = options.hairstyles.filter(
    (item) =>
      item.role === role &&
      item.lengths.includes(length) &&
      (!needsUpdo || item.updo || (length === "short" && item.id === "keep")),
  );
  const suggested = project.default_look_items[role].map((item) => item.hairstyle_id).filter((id) => hairstyles.some((item) => item.id === id));
  // The groom's suggestions all start checked; a bride look is per style, so only the first that fits it.
  const defaults = style ? suggested.slice(0, 1) : suggested;
  const initialPicks = () =>
    Object.fromEntries(hairstyles.map((hairstyle) => [hairstyle.id, defaults.includes(hairstyle.id)])) as Record<string, boolean>;
  const [picks, setPicks] = useState(initialPicks);
  // V7.61: the bride's earrings and necklace for the looks made next; none unless picked.
  const [jewelry, setJewelry] = useState<string | null>(null);
  const suggestedJewelry = style ? options.style_jewelry[style.id] : undefined;
  const [submitting, setSubmitting] = useState(false);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const viewing = project.looks.find((look) => look.id === viewingId) ?? null;
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const looks = project.looks.filter((look) => look.person === which && look.style_id === (style?.id ?? null));
  const inUse = looks.find((look) => look.selected && look.usable);
  // V7.8: a bride look of a style is a hair edit of the style's selected makeup; V7.22: a groom look is a hair
  // edit of his selected retouched photo, which every style shares.
  const makeupInUse = project.makeups.find(
    (makeup) => makeup.person === which && makeup.style_id === (style?.id ?? null) && makeup.selected && makeup.usable,
  );
  const needsMakeup = !makeupInUse;
  const noun = role === "bride" ? "妆容" : "修脸照";

  // Defaults follow the stated hair length and role; reset the list when they change.
  const defaultsKey = JSON.stringify([defaults, hairstyles.map((item) => item.id)]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setPicks(initialPicks()), [defaultsKey]);
  const chosen: LookItem[] = hairstyles
    .filter((hairstyle) => picks[hairstyle.id])
    .map((hairstyle) => ({ hairstyle_id: hairstyle.id, jewelry_id: style ? jewelry : null }));
  const checkAll = (on: (id: string) => boolean) =>
    setPicks((prev) => Object.fromEntries(Object.keys(prev).map((id) => [id, on(id)])));

  const act = async (operation: () => Promise<unknown>) => {
    try {
      await operation();
      await refresh();
    } catch (err) {
      toast.error(err);
    }
  };

  const generate = async (list: LookItem[]) => {
    setSubmitting(true);
    await act(() => api.createLooks(project.id, which, list, style?.id));
    setSubmitting(false);
  };

  const title = style ? `新娘 · ${style.name}` : `${who}的造型（所有风格共用）`;

  return (
    <div className="card looks-card">
      <div className="looks-header">
        <h3>{title}</h3>
        <span className="muted">{inUse ? `选用：${lookLabel(inUse)}` : "还没有选用造型"}</span>
      </div>

      <MakeupPicker
        project={project}
        which={which}
        role={role}
        style={style}
        act={act}
        fitsStyle={(look) => hairstyles.some((item) => item.id === look.hairstyle_id)}
      />

      <h4 className="look-step-title">第二步：在选用的{noun}上换发型</h4>
      <div className="look-form">
        <div className="example-grid hair-examples">
          {hairstyles.map((hairstyle) => (
            <ExampleCard
              key={hairstyle.id}
              src={hairExample(role, hairstyle.id)}
              label={
                <span>
                  {hairstyle.name}
                  {hairstyle.updo && <span className="muted">（盘发）</span>}
                </span>
              }
              on={Boolean(picks[hairstyle.id])}
              onToggle={() => setPicks((prev) => ({ ...prev, [hairstyle.id]: !prev[hairstyle.id] }))}
              onZoom={() => setZoom({ src: hairExample(role, hairstyle.id), caption: `${hairstyle.name}（示例，虚构模特）` })}
            />
          ))}
        </div>
        {style && (
          <div className="field">
            <span className="field-label">首饰（耳饰和项链，戴在造型照上，拍照时照着画；不需要首饰的服饰就选用不戴首饰的造型）</span>
            <div className="count-options jewelry-options">
              {[{ id: null as string | null, name: "不戴" }, ...options.jewelry].map((item) => (
                <button
                  key={item.id ?? "none"}
                  type="button"
                  className={`count-option ${jewelry === item.id ? "active" : ""}`}
                  title={"text" in item ? (item as { text: string }).text : "不戴耳饰和项链"}
                  onClick={() => setJewelry(item.id)}
                >
                  {item.name}
                  {item.id !== null && item.id === suggestedJewelry ? "（建议）" : ""}
                </button>
              ))}
            </div>
            <span className="field-hint">搭配建议：抹胸、方领可选项链组合；高领、挂脖或领口装饰较多时，优先选单独耳饰。</span>
          </div>
        )}
        <div className="look-form-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => checkAll(() => true)}>
            全选（{hairstyles.length} 种）
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => checkAll((id) => defaults.includes(id))}>
            只选建议
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => checkAll(() => false)}>
            清空
          </button>
          <button
            className="btn btn-primary btn-sm"
            onClick={() => generate(chosen)}
            disabled={submitting || chosen.length === 0 || needsMakeup}
          >
            生成已勾选的 {chosen.length} 张（{chosen.length} 次修图）
          </button>
          {needsMakeup && <span className="muted">先在上面生成并选用一张{noun}</span>}
        </div>
      </div>

      {looks.length > 0 && (
        <div className="look-grid">
          {looks.map((look) => {
            const busy = look.status === "waiting" || look.status === "running";
            const used = look.selected && look.usable;
            const sides = project.side_looks.filter((side) => side.parent_look_id === look.id);
            // V7.30: side photos of this person that have no side look made or on its way for this look.
            const person = look.person === "a" ? project.person_a : project.person_b;
            const sideToMake = SIDE_FACINGS.filter(
              (facing) =>
                person[`${facing}_asset_id`] &&
                !sides.some((side) => side.facing === facing && side.status !== "failed" && side.status !== "interrupted"),
            ).length;
            return (
              <div key={look.id} className={`look-tile ${used ? "on" : ""}`}>
                <div className="look-photo">
                  {look.asset_id ? (
                    <img src={assetUrl(look.asset_id)} alt={lookLabel(look)} onClick={() => setViewingId(look.id)} />
                  ) : busy ? (
                    <div className="overlay">
                      <div className="spinner" />
                      <span>{look.status === "running" ? "正在生成" : "排队中"}</span>
                    </div>
                  ) : (
                    <div className="overlay">
                      <div className="error-text">{look.error_message ?? "生成失败"}</div>
                      <button className="btn btn-sm" onClick={() => act(() => api.retryLook(look.id))}>
                        重试
                      </button>
                    </div>
                  )}
                  {used && (
                    <span className="status-chip selected-chip" title={look.selected_by === "auto" ? "相似度最高，自动选用；点别的「选用」可以换" : undefined}>
                      {look.selected_by === "auto" ? "选用中 · 最像" : "选用中"}
                    </span>
                  )}
                </div>
                <div className="look-body">
                  <strong>{lookLabel(look)}</strong>
                  {look.face_score !== null && (
                    <span className="muted">
                      相似度 {look.face_score.toFixed(2)}
                      {look.makeup_face_score !== null && `（${role === "bride" ? "化妆" : "修脸"}后 ${look.makeup_face_score.toFixed(2)}）`}
                    </span>
                  )}
                  {look.unusable_reason && <span className="error-text">{look.unusable_reason}</span>}
                  {makeupInUse && look.makeup_id && look.makeup_id !== makeupInUse.id && (
                    <span className="muted">用的不是当前选用的{noun}</span>
                  )}
                  {(sides.length > 0 || (used && look.status === "ready" && sideToMake > 0)) && (
                    <div className="side-looks">
                      <span className="muted">侧面造型（拍组时侧脸镜头用它）</span>
                      {sides.length > 0 && (
                        <div className="side-look-row">
                          {sides.map((side) => {
                            const facing = FACING_NAMES[side.facing];
                            const sideBusy = side.status === "waiting" || side.status === "running";
                            return (
                              <div key={side.id} className="side-look">
                                {side.asset_id ? (
                                  <img
                                    src={assetUrl(side.asset_id)}
                                    alt={`侧面造型 · ${facing}`}
                                    onClick={() =>
                                      setZoom({ src: assetUrl(side.asset_id!), caption: `${lookLabel(look)} · 侧面造型 · ${facing}` })
                                    }
                                  />
                                ) : (
                                  <div className="side-look-empty" title={side.error_message ?? undefined}>
                                    {sideBusy ? <div className="spinner spinner-sm" /> : <span className="error-text">失败</span>}
                                  </div>
                                )}
                                <span className="muted">{sideBusy ? (side.status === "running" ? "正在生成" : "排队中") : facing}</span>
                                {side.face_score !== null && (
                                  <span className="muted" title="与上传的这张侧脸照比较">
                                    相似度 {side.face_score.toFixed(2)}
                                  </span>
                                )}
                                {!sideBusy && side.asset_id && (
                                  <button
                                    className="btn btn-ghost btn-sm"
                                    title="删除后可以再点「做侧面造型」重做；没做的话，拍组遇到这个角度时会自动补做一张。已经拍好的照片和它们的「重新生成」不受影响"
                                    onClick={() => act(() => api.deleteSideLook(side.id))}
                                  >
                                    删除
                                  </button>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      {used && look.status === "ready" && sideToMake > 0 && (
                        <button
                          className="btn btn-sm"
                          onClick={() => act(() => api.makeSideLooks(look.id))}
                          title="用上传的侧脸照，做成和这套正面造型同样妆发的侧面照；拍组时侧脸镜头用它当参考"
                        >
                          做侧面造型（{sideToMake} 次修图）
                        </button>
                      )}
                    </div>
                  )}
                  <div className="look-actions">
                    {look.status === "ready" && look.usable && (
                      <button
                        className={`btn btn-sm ${look.selected ? "" : "btn-primary"}`}
                        onClick={() => act(() => (look.selected ? api.unselectLook(look.id) : api.selectLook(look.id)))}
                      >
                        {look.selected ? "取消选用" : "选用"}
                      </button>
                    )}
                    {look.status === "ready" && (
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => generate([{ hairstyle_id: look.hairstyle_id, jewelry_id: look.jewelry_id }])}
                        disabled={submitting || needsMakeup || !hairstyles.some((item) => item.id === look.hairstyle_id)}
                        title={`同样的发型，在当前选用的${noun}上再修一张（1 次修图）`}
                      >
                        再来一张
                      </button>
                    )}
                    {!busy && (
                      <button className="btn btn-ghost btn-sm" onClick={() => act(() => api.deleteLook(look.id))}>
                        删除
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {zoom && <Lightbox src={zoom.src} caption={zoom.caption} onClose={() => setZoom(null)} />}
      {viewing?.asset_id && (
        <CompareView
          title={`${title} · ${lookLabel(viewing)}`}
          aligned
          onClose={() => setViewingId(null)}
          items={[
            { key: "source", assetId: viewing.source_asset_id, title: "上传的照片", subtitle: "", actions: [] },
            ...(viewing.makeup_asset_id
              ? [
                  {
                    key: "makeup",
                    assetId: viewing.makeup_asset_id,
                    title: role === "bride" ? "化好妆（改发型之前）" : "修好脸（改发型之前）",
                    subtitle: viewing.makeup_face_score !== null ? `与上传照片的相似度 ${viewing.makeup_face_score.toFixed(2)}` : "",
                    actions: [],
                  },
                ]
              : []),
            {
              key: "look",
              assetId: viewing.asset_id,
              title: lookLabel(viewing),
              subtitle: viewing.face_score !== null ? `与上传照片的相似度 ${viewing.face_score.toFixed(2)}` : "",
              actions: [],
            },
          ]}
        />
      )}
    </div>
  );
}

/** The bride's makeup candidates for one style (V7.8): made on the uploaded photo, hair unchanged; the selected
 * one is what the style's hair candidates are made on. V7.21: a makeup another style already has can be used here
 * too, with the hairstyle photos made on it, without a new edit. V7.22: the groom's retouched photos (no style),
 * which his hairstyles are made on in the same way. */
function MakeupPicker({
  project,
  which,
  role,
  style,
  act,
  fitsStyle,
}: {
  project: ProjectDetail;
  which: "a" | "b";
  role: Role;
  /** The bride's style; none for the groom, whose retouched photo serves every style. */
  style?: Style;
  act: (operation: () => Promise<unknown>) => Promise<void>;
  /** Whether a hairstyle photo can be used by this style (an updo style takes only updos). */
  fitsStyle: (look: Look) => boolean;
}) {
  const [count, setCount] = useState(3);
  const [submitting, setSubmitting] = useState(false);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const viewing = project.makeups.find((makeup) => makeup.id === viewingId) ?? null;
  const noun = role === "bride" ? "妆容" : "修脸照";
  const styleId = style?.id ?? null;
  const makeups = project.makeups.filter((makeup) => makeup.person === which && makeup.style_id === styleId);
  const label = (makeup: Makeup) => `${noun} ${makeups.indexOf(makeup) + 1}`;
  const level = role === "bride" ? project.makeup_level : project.groom_makeup_level;
  const settingsField = role === "bride" ? "bride_retouch_settings" : "groom_retouch_settings";
  const settings = project[settingsField];
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  // Different slimming requests cannot be ranked fairly by likeness alone.
  const shapingGroup = makeups.some((makeup) => makeup.usable && makeup.retouch_settings.slimming !== "off");
  const updateSettings = async (patch: Parameters<typeof api.updateProject>[1]) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      await act(() => api.updateProject(project.id, patch));
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };
  // V7.21: ready makeups of other styles, one per image, that this style does not have yet.
  const ownImages = new Set(makeups.map((makeup) => makeup.asset_id));
  const others: Makeup[] = [];
  for (const makeup of style ? project.makeups : []) {
    if (makeup.person !== which || makeup.style_id === styleId || makeup.status !== "ready" || !makeup.usable) continue;
    if (!makeup.asset_id || ownImages.has(makeup.asset_id) || others.some((other) => other.asset_id === makeup.asset_id)) continue;
    others.push(makeup);
  }
  // The hairstyle photos that come along: made on the same image in any style, usable here, one per image.
  const hairCount = (makeup: Makeup) =>
    new Set(
      project.looks
        .filter((look) => look.person === which && look.makeup_asset_id === makeup.asset_id && look.usable && fitsStyle(look))
        .map((look) => look.asset_id),
    ).size;
  const [reusing, setReusing] = useState<string | null>(null);
  const reuse = async (makeup: Makeup) => {
    if (!style) return;
    setReusing(makeup.id);
    await act(() => api.reuseMakeup(makeup.id, style.id));
    setReusing(null);
  };

  const generate = async (n: number) => {
    setSubmitting(true);
    await act(() => api.createMakeups(project.id, which, styleId, n));
    setSubmitting(false);
  };

  return (
    <div className="makeup-picker">
      <h4 className="look-step-title">
        {role === "bride" ? "第一步：化妆（头发不动），挑一张最好的" : "第一步：修脸（头发不动，所有风格共用），挑一张最好的"}
      </h4>
      <MakeupAdjustments role={role} level={level} settings={settings} disabled={saving || submitting} saving={saving}
        onChange={(next: RetouchSettings) => updateSettings({ [settingsField]: next })}
        onPreset={(next) => updateSettings({ [role === "bride" ? "makeup_level" : "groom_makeup_level"]: next })} />
      {(settings.slimming !== "off" || shapingGroup) && (
        <span className="muted">候选中包含瘦脸调整时，相似度不能单独代表修图效果，系统保留先选用的候选，请对比后自己选最满意的一张。</span>
      )}
      <div className="look-form-actions">
        <label className="muted">
          生成{" "}
          <select value={count} onChange={(event) => setCount(Number(event.target.value))}>
            {[1, 2, 3, 4].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>{" "}
          张
        </label>
        <button className="btn btn-primary btn-sm" onClick={() => generate(count)} disabled={submitting || saving}>
          生成{noun}（{count} 次修图）
        </button>
      </div>
      {makeups.length > 0 && (
        <div className="look-grid">
          {makeups.map((makeup) => {
            const busy = makeup.status === "waiting" || makeup.status === "running";
            const used = makeup.selected && makeup.usable;
            return (
              <div key={makeup.id} className={`look-tile ${used ? "on" : ""}`}>
                <div className="look-photo">
                  {makeup.asset_id ? (
                    <img src={assetUrl(makeup.asset_id)} alt={label(makeup)} onClick={() => setViewingId(makeup.id)} />
                  ) : busy ? (
                    <div className="overlay">
                      <div className="spinner" />
                      <span>{makeup.status === "running" ? "正在生成" : "排队中"}</span>
                    </div>
                  ) : (
                    <div className="overlay">
                      <div className="error-text">{makeup.error_message ?? "生成失败"}</div>
                      <button className="btn btn-sm" onClick={() => act(() => api.retryMakeup(makeup.id))}>
                        重试
                      </button>
                    </div>
                  )}
                  {used && (
                    <span
                      className="status-chip selected-chip"
                      title={
                        makeup.selected_by !== "auto"
                          ? undefined
                          : shapingGroup
                            ? "候选包含瘦脸调整，不按相似度排序，保留先选用的；点别的「选用」可以换"
                            : "相似度最高，自动选用；点别的「选用」可以换"
                      }
                    >
                      {makeup.selected_by !== "auto" ? "选用中" : shapingGroup ? "选用中 · 待对比" : "选用中 · 最像"}
                    </span>
                  )}
                </div>
                <div className="look-body">
                  <strong>{label(makeup)}</strong>
                  <span className="muted">{makeupPresetLabel(makeup.role, makeup.makeup_level)}</span>
                  <span className="muted">{retouchSummary(makeup.retouch_settings)}</span>
                  {style && makeup.makeup_style_id !== style.id && (
                    <span className="muted">复用自「{makeup.makeup_style_name}」的妆</span>
                  )}
                  {makeup.face_score !== null && <span className="muted">相似度 {makeup.face_score.toFixed(2)}</span>}
                  {makeup.unusable_reason && <span className="error-text">{makeup.unusable_reason}</span>}
                  <div className="look-actions">
                    {makeup.status === "ready" && makeup.usable && !makeup.selected && (
                      <button className="btn btn-sm btn-primary" onClick={() => act(() => api.selectMakeup(makeup.id))}>
                        选用
                      </button>
                    )}
                    {!busy && (
                      <button className="btn btn-ghost btn-sm" onClick={() => act(() => api.deleteMakeup(makeup.id))}>
                        删除
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {others.length > 0 && (
        <details className="makeup-reuse">
          <summary>从其他风格复用妆容（{others.length} 张，不花钱）</summary>
          <span className="muted">
            直接用已经生成好的妆容图，不再修图；这张妆容上做过的发型图也会一起带过来（本风格用不了的发型除外）。复用后拍照时按这张图的妆色和妆面来写提示词。
          </span>
          <div className="look-grid">
            {others.map((makeup) => {
              const hair = hairCount(makeup);
              return (
                <div key={makeup.id} className="look-tile">
                  <div className="look-photo">
                    <img
                      src={assetUrl(makeup.asset_id!)}
                      alt={`${makeup.makeup_style_name}的妆容`}
                      onClick={() => setViewingId(makeup.id)}
                    />
                  </div>
                  <div className="look-body">
                    <strong>来自「{makeup.makeup_style_name}」</strong>
                    {makeup.makeup_color_name && <span className="muted">{makeup.makeup_color_name}</span>}
                    <span className="muted">{retouchSummary(makeup.retouch_settings)}</span>
                    {makeup.face_score !== null && <span className="muted">相似度 {makeup.face_score.toFixed(2)}</span>}
                    <span className="muted">{hair > 0 ? `带 ${hair} 张发型图` : "还没有发型图"}</span>
                    <div className="look-actions">
                      <button className="btn btn-sm btn-primary" disabled={reusing !== null} onClick={() => reuse(makeup)}>
                        {reusing === makeup.id ? "复用中…" : "用于本风格"}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </details>
      )}
      {viewing?.asset_id && (
        <CompareView
          title={
            !style
              ? `新郎 · ${label(viewing)}`
              : `新娘 · ${viewing.style_id === style.id ? style.name : viewing.makeup_style_name} · ${
                  viewing.style_id === style.id ? label(viewing) : "可复用的妆容"
                }`
          }
          aligned
          onClose={() => setViewingId(null)}
          items={[
            { key: "source", assetId: viewing.source_asset_id, title: "上传的照片", subtitle: "", actions: [] },
            {
              key: "makeup",
              assetId: viewing.asset_id,
              title: viewing.style_id === styleId ? label(viewing) : `来自「${viewing.makeup_style_name}」`,
              subtitle: `${makeupPresetLabel(viewing.role, viewing.makeup_level)} · ${retouchSummary(viewing.retouch_settings)}${viewing.face_score !== null ? ` · 相似度 ${viewing.face_score.toFixed(2)}` : ""}`,
              actions: [],
            },
          ]}
        />
      )}
    </div>
  );
}

// ---------- Step 3: style previews and shooting sets ----------

function StyleStep({ project, onBack }: { project: ProjectDetail; onBack: () => void }) {
  const { refresh } = useCurrentProject();
  const navigate = useNavigate();
  const toast = useToast();
  const { ask, dialog } = useGenerateDialog();
  const styles = usePolledResource(api.styles, []);
  const lookOptions = usePolledResource(api.lookOptions, []);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const viewing = project.previews.find((slot) => slot.id === viewingId) ?? null;
  const [viewingLook, setViewingLook] = useState<{ assetId: string; caption: string } | null>(null);
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const [chosen, setChosen] = useState<string[] | null>(null);
  /** V7.56: the rooms picked for each chosen style with rooms; a style left out takes its first room without a sample. */
  const [chosenRooms, setChosenRooms] = useState<Record<string, string[]>>({});
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
  const lookById = new Map(project.looks.map((look) => [look.id, look]));

  const idle = (slot: Slot) =>
    Boolean(slot.current_version) && !isGenerating(slot) && slot.latest_task?.status !== "held";

  const menuFor = (slot: Slot): MenuItem[] => [
    {
      label: "不像本人？加强长相重试",
      onSelect: () => actions.run([slot], "identity_boost"),
      disabled: !idle(slot),
    },
    {
      label: "AI 修脸",
      divider: true,
      onSelect: () => actions.faceFix([slot]),
      disabled: !idle(slot),
      title: "用两人的造型照把脸修得更像本人，发型、妆容和美白与造型照一致；每张修一次，按模型计费",
    },
    {
      label: "修手…",
      onSelect: () => actions.handFix(slot),
      disabled: !idle(slot),
      title: "涂出要修的手，只把手放大修好贴回原图，其余保持不变；修一次，按模型计费",
    },
    { label: "局部编辑…", onSelect: () => actions.regionalEdit(slot), disabled: !idle(slot),
      title: "圈出要改的位置，或涂抹蒙版，再描述修改要求；原图保留" },
    {
      label: "洗图（4K）",
      onSelect: () => actions.wash([slot]),
      disabled: !idle(slot),
      title: "整张重绘得更干净清晰，按 4K 输出；洗一次，按模型计费。建议在修脸、修手之后最后再洗",
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
      divider: true,
      onSelect: () => setVersionsFor(slot),
      disabled: slot.version_count === 0,
    },
  ];

  // V7.56: a style has a sample per room, so a style may have several samples.
  const slotByStyle = new Map(project.previews.map((s) => [s.style_id, s]));
  const roomsWithSample = (styleId: string) =>
    project.previews.filter((s) => s.style_id === styleId && s.room).map((s) => s.room!.id);
  /** The set photos shot from this sample's style and room. */
  const shotsOf = (slot: Slot) =>
    project.photos.filter((p) => p.style_id === slot.style_id && (p.room?.id ?? null) === (slot.room?.id ?? null)).length;
  const anyStale = project.previews.some((s) => s.is_stale);
  const allFailed =
    project.previews.length > 0 && project.previews.every((s) => !s.current_version && isBroken(s));

  /** A sample to make: a style, in one of its rooms (V7.56) or without a room. */
  type Target = { styleId: string; roomId: string | null };
  const keyOf = (target: Target) => `${target.styleId}:${target.roomId ?? ""}`;
  const targetOf = (slot: Slot): Target => ({ styleId: slot.style_id, roomId: slot.room?.id ?? null });
  // The samples there are; "regenerate all" covers only these, not every built-in style.
  const existing = project.previews.map(targetOf);
  /** A new sample of a style uses its selected bride look and the groom's selected look (V7.0). */
  const hasLooks = (styleId: string) => Boolean(project.style_looks[styleId]?.bride && project.style_looks[styleId]?.groom);
  // V7.56: first a style, then its outfit (V7.60) and rooms. Every live style stays in the picker, so a scene that has a
  // sample can be shot again, for instance in another outfit.
  const pickable = styles.data.filter((style) => !style.retired);
  const chosenStyles = (chosen ?? (project.previews.length === 0 ? pickable.map((s) => s.id) : [])).filter(
    (id) => hasLooks(id) && pickable.some((style) => style.id === id),
  );
  /** The rooms a chosen style will be shot in: the picked ones, else its first room without a sample. */
  const roomsOf = (style: Style): string[] => {
    const picked = (chosenRooms[style.id] ?? []).filter((id) => style.rooms.some((room) => room.id === id));
    if (picked.length > 0) return picked;
    const sampled = roomsWithSample(style.id);
    return [(style.rooms.find((room) => !sampled.includes(room.id)) ?? style.rooms[0]).id];
  };
  const chosenTargets: Target[] = chosenStyles.flatMap((styleId): Target[] => {
    const style = styles.data!.find((s) => s.id === styleId)!;
    return style.rooms.length === 0 ? [{ styleId, roomId: null }] : roomsOf(style).map((roomId) => ({ styleId, roomId }));
  });
  const toggleRoom = (style: Style, roomId: string) => {
    const current = roomsOf(style);
    const next = current.includes(roomId) ? current.filter((id) => id !== roomId) : [...current, roomId];
    // At least one room stays picked; keep the rooms' own order.
    if (next.length > 0) setChosenRooms({ ...chosenRooms, [style.id]: style.rooms.map((room) => room.id).filter((id) => next.includes(id)) });
  };
  const chooseOutfit = async (styleId: string, outfitId: string) => {
    try {
      await api.chooseStyleOutfit(project.id, styleId, outfitId);
      await refresh();
    } catch (err) {
      toast.error(err);
    }
  };
  const describe = (lookId: string | null | undefined) => (lookId && lookById.get(lookId) ? lookLabel(lookById.get(lookId)!) : "未选");
  const nameOf = (target: Target) => {
    const style = styles.data!.find((s) => s.id === target.styleId);
    const room = style?.rooms.find((r) => r.id === target.roomId);
    return `${style?.name ?? target.styleId}${room ? ` · ${room.name}` : ""}`;
  };

  const regenerate = async (targets: Target[], label: string) => {
    const styleIds = [...new Set(targets.map((target) => target.styleId))];
    const missing = styleIds.filter((styleId) => !hasLooks(styleId)).map((styleId) => styles.data!.find((s) => s.id === styleId)?.name ?? styleId);
    if (missing.length > 0) {
      toast.info(`「${missing.join("」「")}」还没有选用的造型，请先在「风格与造型」里做好并选用`);
      return;
    }
    const lines = styleIds.map((styleId) => {
      const name = styles.data!.find((s) => s.id === styleId)?.name ?? styleId;
      const pair = project.style_looks[styleId];
      return `${name}：新娘 ${describe(pair.bride)}，新郎 ${describe(pair.groom)}`;
    });
    // V7.56: the rooms come from the cards; each picked room of a style is one sample.
    const rooms: Record<string, string[]> = {};
    for (const target of targets) if (target.roomId) (rooms[target.styleId] ??= []).push(target.roomId);
    // V7.60: the outfit is the style's choice, picked in the style picker above its rooms.
    const outfitLines = styleIds.flatMap((styleId) => {
      const ids = lookOptions.data?.style_outfits[styleId] ?? [];
      if (ids.length < 2) return [];
      const name = lookOptions.data!.outfits.find((item) => item.id === project.style_outfits[styleId])?.name;
      return name ? [`${styles.data!.find((s) => s.id === styleId)?.name ?? styleId}穿「${name}」`] : [];
    });
    const again = targets.some((target) => existing.some((item) => keyOf(item) === keyOf(target)));
    const choice = await ask({
      title: label,
      message: `将生成：${targets.map(nameOf).join("、")}。${again ? "重新生成的样片，旧图会保留为历史版本。" : ""}试拍会按所选服饰一起完成穿衣、场景、光线和构图${outfitLines.length > 0 ? `（${outfitLines.join("，")}）` : ""}。使用的妆发——${lines.join("；")}。出图后请先检查人物、服饰和整体效果，满意后再拍组照。`,
      taskCount: targets.length,
    });
    if (!choice) return;
    setBusy(targets.length === 1 ? keyOf(targets[0]) : "all");
    try {
      await api.createPreviews(project.id, {
        styleIds,
        model: choice.model,
        rooms: Object.keys(rooms).length > 0 ? rooms : undefined,
      });
      setChosen([]);
      setChosenRooms({});
      await refresh();
    } catch (err) {
      toast.error(err);
    } finally {
      setBusy(null);
    }
  };

  const retry = async (slot: Slot) => {
    if (!slot.latest_task) return;
    setBusy(slot.id);
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
          每种风格先试拍 1 张：按选好的妆发和服饰，一起完成穿衣、场景、光线和构图。
          放大检查人物是否像本人、服饰是否合适、整体效果是否满意，再点「确认样片，拍一组」。
          要换服饰，在下面选好再试拍；要换妆发，回到「风格与造型」修改后重新试拍；已有样片及用它拍的组照会保留原来的选择。每种风格都可以拍多组。
        </p>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost" onClick={onBack}>
            ← 修改妆发
          </button>
          <button className="btn" onClick={() => (selecting ? exitSelecting() : setSelecting(true))}>
            {selecting ? "退出对比选择" : "对比样片"}
          </button>
          <button
            className="btn"
            onClick={() => regenerate(existing, "重新生成已有的全部样片")}
            disabled={busy !== null || existing.length === 0}
          >
            重新生成全部
          </button>
        </div>
      </div>

      {pickable.length > 0 && (
        <div className="card style-picker">
          <div className="style-picker-header">
            <h3>选择要生成样片的风格</h3>
            <span className="muted">已选 {chosenStyles.length} 种，共 {chosenTargets.length} 张；在下面选每个风格的服饰和场景；没有选用造型的风格要先回上一步做造型</span>
            {chosenStyles.length > 0 && (
              <button className="btn btn-ghost btn-sm" onClick={() => setChosen([])}>
                清空
              </button>
            )}
          </div>
          <div className="example-grid style-examples">
            {pickable.map((style) => {
              const on = chosenStyles.includes(style.id);
              return (
                <ExampleCard
                  key={style.id}
                  src={styleExample(style.id)}
                  label={slotByStyle.has(style.id) ? `${style.name}（已有样片）` : style.name}
                  on={on}
                  disabled={!hasLooks(style.id)}
                  title={hasLooks(style.id) ? style.description : "还没有这个风格的新娘造型，请先在「风格与造型」里做好并选用"}
                  onToggle={() => setChosen(on ? chosenStyles.filter((id) => id !== style.id) : [...chosenStyles, style.id])}
                  onZoom={() => setZoom({ src: styleExample(style.id), caption: `${style.name}（示例）：${style.description}` })}
                />
              );
            })}
          </div>
          {chosenStyles.map((styleId) => {
            const style = styles.data!.find((s) => s.id === styleId)!;
            const outfitIds = lookOptions.data?.style_outfits[style.id] ?? [];
            const picked = roomsOf(style);
            const sampled = roomsWithSample(style.id);
            return (
              <Fragment key={`choice-${style.id}`}>
                {outfitIds.length > 1 && (
                  <div className="field">
                    <span className="field-label">「{style.name}」穿哪套服饰（这个风格的所有场景都穿它；已有样片和组照保持原样）</span>
                    <div className="room-options">
                      {outfitIds.map((id) => {
                        const outfit = lookOptions.data!.outfits.find((item) => item.id === id);
                        const src = `/examples/outfits/${id}.webp`;
                        return (
                          <div className="room-option-wrap" key={id}>
                            <button
                              type="button"
                              className={`room-option outfit-option ${project.style_outfits[style.id] === id ? "active" : ""}`}
                              onClick={() => void chooseOutfit(style.id, id)}
                              title={outfit ? `${outfit.bride}${outfit.groom}` : id}
                            >
                              <img src={src} alt={outfit?.name ?? id} loading="lazy" />
                              <span>{outfit?.name ?? id}</span>
                            </button>
                            <button
                              type="button"
                              className="example-zoom"
                              aria-label="看大图"
                              title="看大图"
                              onClick={() => setZoom({ src, caption: outfit ? `${outfit.name}：${outfit.bride}${outfit.groom}${outfit.headwear}` : id })}
                            >
                              ⤢
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
                {style.rooms.length > 0 && (
                  <div className="field">
                    <span className="field-label">「{style.name}」在哪些场景拍（可多选，每个场景一张样片）</span>
                    <div className="room-options">
                      {style.rooms.map((room) => (
                        <div className="room-option-wrap" key={room.id}>
                          <button
                            type="button"
                            className={`room-option ${picked.includes(room.id) ? "active" : ""}`}
                            onClick={() => toggleRoom(style, room.id)}
                            title={room.areas.map((area) => area.name).join("、")}
                          >
                            <img src={room.areas[0].image_url ?? undefined} alt={room.name} loading="lazy" />
                            <span>{room.name}{sampled.includes(room.id) ? "（已有样片）" : ""}</span>
                          </button>
                          {room.areas[0].image_url && (
                            <button
                              type="button"
                              className="example-zoom"
                              aria-label="看大图"
                              title="看大图"
                              onClick={() => setZoom({ src: room.areas[0].image_url!, caption: `${style.name} · ${room.name}` })}
                            >
                              ⤢
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </Fragment>
            );
          })}
          <div className="style-picker-actions">
            <button
              className="btn btn-primary"
              disabled={chosenTargets.length === 0 || busy !== null}
              onClick={() => regenerate(chosenTargets, "生成风格样片")}
            >
              生成所选的样片（{chosenTargets.length} 张）
            </button>
          </div>
        </div>
      )}

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
            <button className="btn btn-primary" onClick={() => regenerate(existing, "重新生成全部样片")}>
              全部重试
            </button>
          }
        />
      ) : (
        <div className="preview-grid">
          {project.previews.map((slot) => {
            const style = styles.data!.find((item) => item.id === slot.style_id);
            if (!style) return null;
            return (
              <PreviewCard
                key={slot.id}
                style={style}
                slot={slot}
                shotCount={shotsOf(slot)}
                busy={busy === keyOf(targetOf(slot)) || busy === slot.id || busy === "all"}
                onView={(slot) => setViewingId(slot.id)}
                onRegenerate={() => regenerate([targetOf(slot)], `重新生成「${nameOf(targetOf(slot))}」样片`)}
                looks={(() => {
                  // The looks the style uses now (what the next sample is shot from); the sample's own looks where none
                  // is selected, and its styled photos for pictures from before looks.
                  const now = project.style_looks[style.id];
                  return (["bride", "groom"] as const).map((role) => {
                    const lookId = now?.[role] ?? slot.look_ids?.[role];
                    const look = lookId ? lookById.get(lookId) : undefined;
                    const assetId = look?.asset_id ?? slot.styled_asset_ids?.[role] ?? null;
                    return { role, assetId, label: look ? lookLabel(look) : null };
                  });
                })()}
                looksChanged={(() => {
                  const own = slot.look_ids;
                  const now = project.style_looks[style.id];
                  return Boolean(own && now?.bride && now?.groom && (own.bride !== now.bride || own.groom !== now.groom));
                })()}
                onViewLook={(assetId, caption) => setViewingLook({ assetId, caption: `${style.name} · ${caption}` })}
                onRetry={retry}
                onShoot={shoot}
                menu={menuFor(slot)}
                onOpenAlbum={() => navigate(`/projects/${project.id}/album?style=${style.id}`)}
                selecting={selecting}
                selected={picked.includes(slot.id)}
                onToggle={togglePick}
                onCompareSource={setSourceCompare}
              />
            );
          })}
        </div>
      )}

      {viewing?.current_version && (
        <Lightbox
          src={assetUrl(viewing.current_version.asset_id)}
          caption={[
            viewing.style_name,
            `V${viewing.current_version.version_number}`,
            viewing.current_version.model,
            faceScoreText(viewing.current_version.face_scores),
          ]
            .filter(Boolean)
            .join(" · ")}
          onClose={() => setViewingId(null)}
        />
      )}
      {zoom && <Lightbox src={zoom.src} caption={zoom.caption} onClose={() => setZoom(null)} />}
      {viewingLook && (
        <Lightbox src={assetUrl(viewingLook.assetId)} caption={viewingLook.caption} onClose={() => setViewingLook(null)} />
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
            const shot = shotsOf(slot);
            return {
              key: slot.id,
              assetId: slot.current_version!.asset_id,
              scores: slot.current_version!.face_scores,
              title: `${slot.style_name}${slot.room ? ` · ${slot.room.name}` : ""}`,
              subtitle: [`V${slot.current_version!.version_number}`, slot.current_version!.model]
                .filter(Boolean)
                .join(" · "),
              actions: [
                {
                  label: shot > 0 ? "再拍一组" : "确认样片，拍一组",
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
          shotCount={shotsOf(planning)}
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
  looks,
  looksChanged,
  onViewLook,
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
  looks: { role: Role; assetId: string | null; label: string | null }[] | null;
  /** The style's selected looks are no longer the ones this sample was shot from. */
  looksChanged: boolean;
  onViewLook: (assetId: string, caption: string) => void;
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
              <span>已暂停，等待你在任务中心继续</span>
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
          <button className="status-chip swap-chip" onClick={() => onCompareSource(slot)} title={`与${fixLabel(slot)}前的原图对比`}>
            {fixLabel(slot)} · 对比
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
          <h3>{style.name}{slot?.room ? ` · ${slot.room.name}` : ""}</h3>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            {version?.model && <span className="tag">{version.model}</span>}
            {version && slot && slot.version_count > 1 && <span className="tag">V{version.version_number}</span>}
            {menu && <SlotMenu items={menu} />}
          </div>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>
          {style.description}
        </p>
        {slot?.room && <span className="field-hint">场景：{slot.room.name}（整组在这个场景里拍）</span>}
        {slot?.style_art_asset_id && (
          <a className="preview-art" href={assetUrl(slot.style_art_asset_id)} target="_blank" rel="noreferrer" title="这张样片用的服饰场景参考图（衣服和场景照它画，图里没有人）">
            <img src={assetUrl(slot.style_art_asset_id)} alt="服饰场景参考图" loading="lazy" />
            <span className="field-hint">服饰场景参考图：衣服和场景照它画，点开检查</span>
          </a>
        )}
        <FaceScore scores={version?.face_scores} />
        {version?.size_warning && <span className="error-text">尺寸不符：{version.size_warning}</span>}
        {looks && looks.some((item) => item.assetId) && (
          <div className="styled-row">
            {looks.map(({ role, assetId, label }) =>
              assetId ? (
                <button
                  key={role}
                  className="styled-thumb"
                  onClick={() => onViewLook(assetId, `${ROLE_LABEL[role]}${label ? `：${label}` : "的造型照"}`)}
                  title={`${ROLE_LABEL[role]}${label ? `：${label}` : "的造型照"}`}
                >
                  <img src={assetUrl(assetId)} alt={`${ROLE_LABEL[role]}的造型`} />
                </button>
              ) : null,
            )}
            <div className="styled-info">
              <span className="muted">{looks.find((item) => item.role === "bride")?.label ?? "造型照"}</span>
              {looksChanged && <span className="muted">造型已更换：这里是新造型，样片还是旧造型拍的，重新生成后才会用新造型</span>}
            </div>
          </div>
        )}
        <div className="preview-actions">
          <button className="btn btn-sm" onClick={onRegenerate} disabled={busy || generating}>
            {slot ? "重新生成" : "生成样片"}
          </button>
          <button
            className={`btn btn-sm ${shotCount > 0 ? "btn-gold" : "btn-primary"}`}
            onClick={() => slot && onShoot(slot)}
            disabled={!version || busy}
          >
            {shotCount > 0 ? "再拍一组" : "确认样片，拍一组"}
          </button>
        </div>
      </div>
    </div>
  );
}

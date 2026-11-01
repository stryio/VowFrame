import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  api,
  assetUrl,
  isBroken,
  isFaceSwapped,
  isGenerating,
  type BrideHairLength, type Facing, type GroomHairLength, type Look, type LookItem, type LookOptions,
  type MakeupLevel, type Person,
  type ProjectDetail,
  type Role,
  type Slot,
  type Style,
} from "../api";
import { AttemptNotice } from "../components/AttemptNotice";
import { CompareView } from "../components/CompareView";
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
          <p>先选风格、做好两人的造型，再试拍样片，喜欢哪种风格就用它拍一组，可以拍多组。</p>
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
      {step === "looks" && <LooksStep project={project} onBack={() => setStep("upload")} onNext={() => setStep("style")} />}
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

const BRIDE_MAKEUP_OPTIONS: [MakeupLevel, string][] = [
  ["light", "清透新娘妆"],
  ["natural", "标准婚纱妆"],
  ["refined", "精致影楼妆"],
];

const GROOM_MAKEUP_OPTIONS: [MakeupLevel, string][] = [
  ["light", "清爽"],
  ["natural", "精修"],
  ["refined", "精致"],
];

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
        {row("makeup_level", "新娘妆容", BRIDE_MAKEUP_OPTIONS)}
        {row("groom_makeup_level", "新郎精修", GROOM_MAKEUP_OPTIONS)}
        <span className="muted">
          头发长度请按照片里的实际长度选，它决定能做哪些发型，长度保持和照片一致。妆容档位决定造型照的化妆、提白和去瑕疵程度，越高修得越多；
          新郎只在「精致」一档化淡妆；新娘的妆色和妆面按风格配。改了只影响之后生成的造型，已经生成的不变。
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
  const sideInputRefs = { left: useRef<HTMLInputElement>(null), right: useRef<HTMLInputElement>(null) };
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
        {(["left", "right"] as const).map((angle) => {
          const assetId = angle === "left" ? person.left_asset_id : person.right_asset_id;
          const label = angle === "left" ? "脸朝画面左" : "脸朝画面右";
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
    </div>
  );
}

// ---------- Step 2: styles and looks (V5.0; per style since V7.0) ----------

const lookLabel = (look: Look) => look.hairstyle_name;

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
  // Styles picked for the bride that have no look yet; styles with looks are always shown.
  const [picked, setPicked] = useState<string[]>([]);
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const ready = hasStyleLooks(project);
  const bride = project.person_a.role === "bride" ? "a" : "b";
  const groom = bride === "a" ? "b" : "a";

  if (!options.data || !styles.data) return <div className="muted">加载中…</div>;

  const withLooks = new Set(project.looks.filter((look) => look.person === bride && look.style_id).map((look) => look.style_id));
  const sampled = new Set(project.previews.map((slot) => slot.style_id));
  // A retired style may only refresh a sample this project already has.
  const offered = styles.data.filter((style) => !style.retired || sampled.has(style.id));
  const chosen = offered.filter((style) => withLooks.has(style.id) || picked.includes(style.id));

  return (
    <>
      <div className="page-header">
        <p className="muted">
          新娘每个风格做一套造型：发型从适合这个风格的里面选，妆色和妆面按风格配；新郎做一套，所有风格共用。
          每张造型候选是从上传的照片修一次（一次付费修图），发型和妆容一起做好，背景换成纯色。
          每个风格用选用的那套拍样片和成片；第一套生成好的自动选用，可以换。相似度越接近 1 越像本人，偏低的建议再来一张。
        </p>
      </div>

      <MakeupCard project={project} />

      <PersonLooks project={project} which={groom} role="groom" options={options.data} />

      <div className="card style-picker">
        <div className="style-picker-header">
          <h3>新娘：选择要拍的风格</h3>
          <span className="muted">已选 {chosen.length} 种；每种风格做一套新娘造型</span>
        </div>
        <div className="example-grid style-examples">
          {offered.map((style) => {
            const on = chosen.some((item) => item.id === style.id);
            const locked = withLooks.has(style.id);
            return (
              <ExampleCard
                key={style.id}
                src={styleExample(style.id)}
                label={style.name}
                on={on}
                disabled={locked}
                title={locked ? "这个风格已经有造型；删掉它的全部造型后可以取消" : style.description}
                onToggle={() => setPicked(on ? picked.filter((id) => id !== style.id) : [...picked, style.id])}
                onZoom={() => setZoom({ src: styleExample(style.id), caption: `${style.name}（示例）：${style.description}` })}
              />
            );
          })}
        </div>
        <span className="muted">示例图里是虚构的模特，用来看风格的场景、服装和光线。</span>
      </div>
      {zoom && <Lightbox src={zoom.src} caption={zoom.caption} onClose={() => setZoom(null)} />}

      {chosen.map((style) => (
        <PersonLooks key={style.id} project={project} which={bride} role="bride" options={options.data!} style={style} />
      ))}

      <div className="action-bar">
        <button className="btn btn-ghost" onClick={onBack}>
          ← 修改照片
        </button>
        {!ready && <span className="muted">请选用新郎的造型和至少一个风格的新娘造型</span>}
        <button className="btn btn-primary btn-lg" disabled={!ready} onClick={onNext}>
          下一步：试拍风格 →
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
  const [submitting, setSubmitting] = useState(false);
  const [viewing, setViewing] = useState<Look | null>(null);
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const looks = project.looks.filter((look) => look.person === which && look.style_id === (style?.id ?? null));
  const inUse = looks.find((look) => look.selected && look.usable);

  // Defaults follow the stated hair length and role; reset the list when they change.
  const defaultsKey = JSON.stringify([defaults, hairstyles.map((item) => item.id)]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setPicks(initialPicks()), [defaultsKey]);
  const chosen: LookItem[] = hairstyles.filter((hairstyle) => picks[hairstyle.id]).map((hairstyle) => ({ hairstyle_id: hairstyle.id }));
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
            disabled={submitting || chosen.length === 0}
          >
            生成已勾选的 {chosen.length} 张（{chosen.length} 次修图）
          </button>
        </div>
      </div>

      {looks.length > 0 && (
        <div className="look-grid">
          {looks.map((look) => {
            const busy = look.status === "waiting" || look.status === "running";
            const used = look.selected && look.usable;
            return (
              <div key={look.id} className={`look-tile ${used ? "on" : ""}`}>
                <div className="look-photo">
                  {look.asset_id ? (
                    <img src={assetUrl(look.asset_id)} alt={lookLabel(look)} onClick={() => setViewing(look)} />
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
                  {used && <span className="status-chip selected-chip">选用中</span>}
                </div>
                <div className="look-body">
                  <strong>{lookLabel(look)}</strong>
                  {look.face_score !== null && <span className="muted">相似度 {look.face_score.toFixed(2)}</span>}
                  {look.unusable_reason && <span className="error-text">{look.unusable_reason}</span>}
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
                        onClick={() => generate([{ hairstyle_id: look.hairstyle_id }])}
                        disabled={submitting || !hairstyles.some((item) => item.id === look.hairstyle_id)}
                        title="同样的发型再修一张（1 次修图）"
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
          onClose={() => setViewing(null)}
          items={[
            { key: "source", assetId: viewing.source_asset_id, title: "上传的照片", subtitle: "", actions: [] },
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

// ---------- Step 3: style previews and shooting sets ----------

function StyleStep({ project, onBack }: { project: ProjectDetail; onBack: () => void }) {
  const { refresh } = useCurrentProject();
  const navigate = useNavigate();
  const toast = useToast();
  const { ask, dialog } = useGenerateDialog();
  const styles = usePolledResource(api.styles, []);
  const [viewing, setViewing] = useState<Slot | null>(null);
  const [viewingLook, setViewingLook] = useState<{ assetId: string; caption: string } | null>(null);
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const [chosen, setChosen] = useState<string[] | null>(null);
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
      onSelect: () => actions.faceFix([slot]),
      disabled: !idle(slot) || !faceSwap?.available,
      title:
        faceSwap && !faceSwap.available
          ? (faceSwap.reason ?? "AI 修脸需要 FaceFusion 找脸")
          : "用上传的原照把脸修得更像本人，保留妆容和精修；每张修两次，按模型计费",
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

  // Styles that already have a sample; "regenerate all" covers only these, not every built-in style.
  const generatedStyleIds = styles.data.filter((s) => slotByStyle.has(s.id)).map((s) => s.id);
  /** A new sample of a style uses its selected bride look and the groom's selected look (V7.0). */
  const hasLooks = (styleId: string) => Boolean(project.style_looks[styleId]?.bride && project.style_looks[styleId]?.groom);
  const pickable = styles.data.filter((style) => !style.retired && !slotByStyle.has(style.id));
  const chosenStyles = (chosen ?? (project.previews.length === 0 ? pickable.map((s) => s.id) : [])).filter(hasLooks);
  const describe = (lookId: string | null | undefined) => (lookId && lookById.get(lookId) ? lookLabel(lookById.get(lookId)!) : "未选");

  const regenerate = async (styleIds: string[], label: string) => {
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
    const choice = await ask({
      title: label,
      message: `${generatedStyleIds.some((id) => styleIds.includes(id)) ? "已有的样片会保留为历史版本。" : ""}使用的造型——${lines.join("；")}。`,
      taskCount: styleIds.length,
    });
    if (!choice) return;
    setBusy(styleIds.length === 1 ? styleIds[0] : "all");
    try {
      await api.createPreviews(project.id, { styleIds, model: choice.model });
      setChosen([]);
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
          每种风格先生成 1 张样片，用这个风格选用的新娘造型和新郎造型拍，妆容照着造型照。喜欢哪种，就点「用这个风格拍一组」；
          可以拍多种风格，也可以同一风格多拍几组。要换造型，回到「风格与造型」选用另一套，再重新生成样片。
        </p>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost" onClick={onBack}>
            ← 修改造型
          </button>
          <button className="btn" onClick={() => (selecting ? exitSelecting() : setSelecting(true))}>
            {selecting ? "退出对比选择" : "对比样片"}
          </button>
          <button
            className="btn"
            onClick={() => regenerate(generatedStyleIds, "重新生成已有的全部样片")}
            disabled={busy !== null || generatedStyleIds.length === 0}
          >
            重新生成全部
          </button>
        </div>
      </div>

      {pickable.length > 0 && (
        <div className="card style-picker">
          <div className="style-picker-header">
            <h3>选择要生成样片的风格</h3>
            <span className="muted">已选 {chosenStyles.length} 种；没有选用造型的风格要先回上一步做造型</span>
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
                  label={style.name}
                  on={on}
                  disabled={!hasLooks(style.id)}
                  title={hasLooks(style.id) ? style.description : "还没有这个风格的新娘造型，请先在「风格与造型」里做好并选用"}
                  onToggle={() => setChosen(on ? chosenStyles.filter((id) => id !== style.id) : [...chosenStyles, style.id])}
                  onZoom={() => setZoom({ src: styleExample(style.id), caption: `${style.name}（示例）：${style.description}` })}
                />
              );
            })}
          </div>
          <div className="style-picker-actions">
            <button
              className="btn btn-primary"
              disabled={chosenStyles.length === 0 || busy !== null}
              onClick={() => regenerate(chosenStyles, "生成风格样片")}
            >
              生成所选风格的样片（{chosenStyles.length} 张）
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
            <button className="btn btn-primary" onClick={() => regenerate(generatedStyleIds, "重新生成全部样片")}>
              全部重试
            </button>
          }
        />
      ) : (
        <div className="preview-grid">
          {styles.data.filter((style) => slotByStyle.has(style.id)).map((style) => (
            <PreviewCard
              key={style.id}
              style={style}
              slot={slotByStyle.get(style.id)}
              shotCount={setByStyle.get(style.id)?.slot_count ?? 0}
              busy={busy === style.id || busy === "all"}
              onView={setViewing}
              onRegenerate={() => regenerate([style.id], `${slotByStyle.get(style.id) ? "重新生成" : "生成"}「${style.name}」样片`)}
              looks={(() => {
                const slot = slotByStyle.get(style.id);
                if (!slot) return null;
                return (["bride", "groom"] as const).map((role) => {
                  const look = slot.look_ids ? lookById.get(slot.look_ids[role]) : undefined;
                  const assetId = look?.asset_id ?? slot.styled_asset_ids?.[role] ?? null;
                  return { role, assetId, label: look ? lookLabel(look) : null };
                });
              })()}
              looksChanged={(() => {
                const own = slotByStyle.get(style.id)?.look_ids;
                const now = project.style_looks[style.id];
                return Boolean(own && now?.bride && now?.groom && (own.bride !== now.bride || own.groom !== now.groom));
              })()}
              onViewLook={(assetId, caption) => setViewingLook({ assetId, caption: `${style.name} · ${caption}` })}
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
            faceScoreText(viewing.current_version.face_scores),
          ]
            .filter(Boolean)
            .join(" · ")}
          onClose={() => setViewing(null)}
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
            const shot = setByStyle.get(slot.style_id)?.slot_count ?? 0;
            return {
              key: slot.id,
              assetId: slot.current_version!.asset_id,
              scores: slot.current_version!.face_scores,
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
          <button className="status-chip swap-chip" onClick={() => onCompareSource(slot)} title="与修脸前的原图对比">
            修脸 · 对比
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
        <FaceScore scores={version?.face_scores} />
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
              {looksChanged && <span className="muted">造型已更换，重新生成会用新的造型</span>}
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
            {shotCount > 0 ? "再拍一组" : "用这个风格拍一组"}
          </button>
        </div>
      </div>
    </div>
  );
}

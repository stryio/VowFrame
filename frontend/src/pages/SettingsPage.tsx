import { useEffect, useState } from "react";
import {
  api,
  type AutoFaceFix,
  type BatchReferenceMode,
  type Health,
  type ImageProvider,
  type ImageProviderId,
  type Model,
  type PortraitRatio,
} from "../api";
import { EmptyState, useToast } from "../components/feedback";

const AUTO_FACE_FIX_OPTIONS: [AutoFaceFix, string][] = [
  ["split", "新娘 AI 修脸 + 新郎 FaceFusion"],
  ["ai", "两人都 AI 修脸"],
  ["facefusion", "两人都 FaceFusion 换脸"],
  ["off", "不修"],
];

export function SettingsPage() {
  const toast = useToast();
  const [health, setHealth] = useState<Health | null>(null);
  const [models, setModels] = useState<Model[] | null>(null);
  const [defaultModel, setDefaultModel] = useState("");
  const [provider, setProvider] = useState<ImageProviderId>("88api");
  const [providers, setProviders] = useState<ImageProvider[]>([]);
  const [referenceMode, setReferenceMode] = useState<BatchReferenceMode>("prompt_only");
  const [portraitRatio, setPortraitRatio] = useState<PortraitRatio>("3:4");
  const [autoFaceFix, setAutoFaceFix] = useState<AutoFaceFix>("split");
  const [directorModel, setDirectorModel] = useState("");
  const [savedDirector, setSavedDirector] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([api.health(), api.models(), api.settings()])
      .then(([h, list, settings]) => {
        setHealth(h);
        setModels(list);
        setDefaultModel(settings.default_model);
        setProvider(settings.image_provider);
        setProviders(settings.image_providers);
        setReferenceMode(settings.batch_reference_mode);
        setPortraitRatio(settings.portrait_ratio);
        setAutoFaceFix(settings.auto_face_fix);
        setDirectorModel(settings.director_model ?? "");
        setSavedDirector(settings.director_model);
      })
      .catch(() => setFailed(true));
  }, []);

  const changeModel = async (id: string) => {
    const previous = defaultModel;
    setDefaultModel(id);
    setSaving(true);
    try {
      const saved = await api.updateSettings({ default_model: id });
      setDefaultModel(saved.default_model);
      setHealth((h) => (h ? { ...h, model: saved.default_model } : h));
      toast.info("默认模型已更新，新建的任务将使用它");
    } catch (err) {
      setDefaultModel(previous);
      toast.error(err);
    } finally {
      setSaving(false);
    }
  };

  // V7.11: switching the service also lists its models; the backend keeps a default model it offers.
  const changeProvider = async (id: ImageProviderId) => {
    setSaving(true);
    try {
      const saved = await api.updateSettings({ image_provider: id });
      setProvider(saved.image_provider);
      setModels(await api.models());
      if (saved.default_model !== defaultModel) {
        toast.info(`已切换到 ${providers.find((item) => item.id === id)?.name}，默认模型改为它提供的 ${saved.default_model}`);
      } else {
        toast.info(`已切换到 ${providers.find((item) => item.id === id)?.name}，新建的任务将发给它`);
      }
      setDefaultModel(saved.default_model);
      setHealth((h) => (h ? { ...h, model: saved.default_model } : h));
    } catch (err) {
      toast.error(err);
    } finally {
      setSaving(false);
    }
  };

  const changeReferenceMode = async (mode: BatchReferenceMode) => {
    const previous = referenceMode;
    setReferenceMode(mode);
    setSaving(true);
    try {
      const saved = await api.updateSettings({ batch_reference_mode: mode });
      setReferenceMode(saved.batch_reference_mode);
      toast.info("拍组参考方式已更新，之后新拍的组将使用它");
    } catch (err) {
      setReferenceMode(previous);
      toast.error(err);
    } finally {
      setSaving(false);
    }
  };

  const changeAutoFaceFix = async (mode: AutoFaceFix) => {
    const previous = autoFaceFix;
    setAutoFaceFix(mode);
    setSaving(true);
    try {
      const saved = await api.updateSettings({ auto_face_fix: mode });
      setAutoFaceFix(saved.auto_face_fix);
      toast.info(`之后生成的照片：${AUTO_FACE_FIX_OPTIONS.find(([value]) => value === saved.auto_face_fix)?.[1]}`);
    } catch (err) {
      setAutoFaceFix(previous);
      toast.error(err);
    } finally {
      setSaving(false);
    }
  };

  const changePortraitRatio = async (ratio: PortraitRatio) => {
    const previous = portraitRatio;
    setPortraitRatio(ratio);
    setSaving(true);
    try {
      const saved = await api.updateSettings({ portrait_ratio: ratio });
      setPortraitRatio(saved.portrait_ratio);
      toast.info("画幅比例已更新，之后新生成的照片将使用它");
    } catch (err) {
      setPortraitRatio(previous);
      toast.error(err);
    } finally {
      setSaving(false);
    }
  };

  const saveDirector = async () => {
    const value = directorModel.trim();
    setSaving(true);
    try {
      const saved = await api.updateSettings({ director_model: value || null });
      setSavedDirector(saved.director_model);
      setDirectorModel(saved.director_model ?? "");
      toast.info(saved.director_model ? `AI 导演将使用 ${saved.director_model}` : "已清除 AI 导演模型");
    } catch (err) {
      toast.error(err);
    } finally {
      setSaving(false);
    }
  };

  const selected = models?.find((m) => m.id === defaultModel);

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>设置</h1>
          <p>默认模型修改后立即生效，只影响之后新建的任务。</p>
        </div>
      </div>

      <div
        className="card"
        style={{
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        <h3>本地服务</h3>
        {failed ? (
          <span className="error-text">无法连接本地服务，请确认后端已启动</span>
        ) : !health ? (
          <span className="muted">检测中…</span>
        ) : health.generator === "mock" ? (
          <>
            <span className="muted">运行中 · 生图方式：模拟生成（不调用模型，原样返回参考照片）</span>
            <div className="notice">
              要使用真实模型，请按 backend/README.md 创建 backend/config.local.json 并填写 API Key，然后重启后端。
            </div>
          </>
        ) : (
          <span className="muted">运行中 · 生图服务：{health.generator}</span>
        )}
      </div>

      {health && (
        <div
          className="card"
          style={{
            padding: 24,
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          <h3>修脸（AI 修脸 / FaceFusion 换脸）</h3>
          <p className="field-hint">
            用两人的造型照修正照片里的脸，构图、服装和光线不变；原图保留在历史版本中，可随时「对比原图」或恢复原图。
          </p>
          <div className="field">
            <span className="field-label">生成后自动修脸</span>
            <div className="shot-switch" role="group" aria-label="生成后自动修脸">
              {AUTO_FACE_FIX_OPTIONS.map(([value, label]) => (
                <button
                  key={value}
                  className={`shot-pill ${autoFaceFix === value ? "active" : ""}`}
                  disabled={saving || autoFaceFix === value}
                  onClick={() => changeAutoFaceFix(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <span className="field-hint">
              每张有人脸的样片和相册照片生成后自动做一次。默认（新娘 AI + 新郎 FaceFusion）：先用 AI 只修新娘，保住她的妆容和美白，每张按默认模型计费一次；再用 FaceFusion 只换新郎的脸，本地处理、不花钱，不会动新郎的发型。没有配置 FaceFusion 时只修新娘。AI 修脸会把整张图重画一遍，FaceFusion 换脸会把妆洗淡一些。照片菜单里的「AI 修脸」「换脸修正」仍是修两个人。
            </span>
          </div>
          <span className="field-label">FaceFusion 换脸</span>
          {health.face_swap.available ? (
            <>
              <span className="muted">
                可用 ·{" "}
                {health.face_swap.mode === "mock"
                  ? "模拟换脸（原样复制图片，用于测试）"
                  : "FaceFusion · 正脸 hyperswap_1c_256 · 侧脸 hyperswap_1b_256 · 像素提升 512 · 遮罩 box + occlusion · 强度：相册 0.5，样片 1.0"}
              </span>
            </>
          ) : (
            <>
              <span className="error-text">不可用：{health.face_swap.reason}</span>
              <span className="field-hint">
                在 backend/config.local.json 中填写 "facefusion_dir"（FaceFusion 安装目录），然后重启后端。
              </span>
            </>
          )}
        </div>
      )}

      {models && (
        <div
          className="card"
          style={{
            padding: 24,
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          <h3>模型</h3>
          <label className="field" style={{ maxWidth: 420 }}>
            <span className="field-label">生图服务</span>
            <select
              className="input"
              value={provider}
              disabled={saving}
              onChange={(e) => changeProvider(e.target.value as ImageProviderId)}
            >
              {providers.map((item) => (
                <option key={item.id} value={item.id} disabled={!item.available}>
                  {item.name}
                  {item.available ? "" : "（未配置）"}
                </option>
              ))}
            </select>
            {providers
              .filter((item) => !item.available && item.reason)
              .map((item) => (
                <span key={item.id} className="field-hint">
                  {item.name}：{item.reason}
                </span>
              ))}
            <span className="field-hint">
              妆容、造型、样片、成片和 AI 修脸都发给这个服务；AI 导演写分镜仍用 88API。切换前已经在排队的任务，若用的模型只有另一个服务提供，仍发给那个服务。
            </span>
          </label>
          <label className="field" style={{ maxWidth: 420 }}>
            <span className="field-label">默认模型</span>
            <select
              className="input"
              value={defaultModel}
              disabled={saving}
              onChange={(e) => changeModel(e.target.value)}
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
            {selected && <span className="field-hint">{selected.description}</span>}
          </label>
          <span className="field-hint">生成样片或拍组时，也可以在确认弹窗里为这一次临时换用其他模型。</span>
        </div>
      )}

      {models && (
        <div
          className="card"
          style={{
            padding: 24,
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          <h3>AI 导演</h3>
          <p className="field-hint">
            拍组和换动作、换景别时，由这个文字模型设计每张照片的镜头：机位、构图、光线和动作瞬间。只发送风格和场景文字，不发送照片。
          </p>
          <div style={{ display: "flex", gap: 8, maxWidth: 520 }}>
            <input
              className="input"
              style={{ flex: 1 }}
              placeholder="88API 上的文字模型名称，例如 gpt-6.1-sol"
              value={directorModel}
              disabled={saving}
              onChange={(e) => setDirectorModel(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && saveDirector()}
            />
            <button
              className="btn btn-primary"
              onClick={saveDirector}
              disabled={saving || directorModel.trim() === (savedDirector ?? "")}
            >
              保存
            </button>
          </div>
          {health?.generator === "mock" ? (
            <span className="field-hint">当前是模拟生图模式，使用模拟导演，不调用文字模型。</span>
          ) : (
            !savedDirector && <span className="error-text">尚未设置：拍组前请先填写 AI 导演模型。</span>
          )}
        </div>
      )}

      {models && (
        <div
          className="card"
          style={{
            padding: 24,
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          <h3>拍组参考方式</h3>
          <div className="radio-group">
            <label className={`radio-card ${referenceMode === "prompt_only" ? "active" : ""}`}>
              <input
                type="radio"
                name="reference-mode"
                checked={referenceMode === "prompt_only"}
                disabled={saving}
                onChange={() => changeReferenceMode("prompt_only")}
              />
              <span className="field-label">只用两人照片（推荐）</span>
              <span className="field-hint">
                风格靠文字描述。样片里的人脸本身是模型的近似，不传给模型，成片更容易像本人；AI
                导演可以为每张选不同的场景，整组更有变化；但同一组内服装、场景细节可能略有差异。
              </span>
            </label>
            <label className={`radio-card ${referenceMode === "with_preview" ? "active" : ""}`}>
              <input
                type="radio"
                name="reference-mode"
                checked={referenceMode === "with_preview"}
                disabled={saving}
                onChange={() => changeReferenceMode("with_preview")}
              />
              <span className="field-label">两人照片 + 样片</span>
              <span className="field-hint">
                把样片作为第三张参考图，风格更统一，所有照片都在样片的场景里拍；但模型可能参考样片里的脸而偏离本人长相，也可能照搬样片构图。
              </span>
            </label>
          </div>
          <span className="field-hint">
            只影响之后新拍的组；已经创建的任务保持不变。哪种更好，建议用同一风格各拍一组对比。
          </span>
        </div>
      )}

      {models && (
        <div
          className="card"
          style={{
            padding: 24,
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          <h3>画幅比例</h3>
          <div className="radio-group">
            {(
              [
                ["3:4", "竖版 3:4 · 横版 4:3（推荐）", "相册内页、冲印和摆台最常用。"],
                ["2:3", "竖版 2:3 · 横版 3:2", "和相机原片一致的经典比例，竖版更修长。"],
                ["9:16", "竖版 9:16 · 横版 16:9", "适合手机壁纸、朋友圈和短视频封面，冲印时裁切较多。"],
              ] as const
            ).map(([ratio, label, hint]) => (
              <label key={ratio} className={`radio-card ${portraitRatio === ratio ? "active" : ""}`}>
                <input
                  type="radio"
                  name="portrait-ratio"
                  checked={portraitRatio === ratio}
                  disabled={saving}
                  onChange={() => changePortraitRatio(ratio)}
                />
                <span className="field-label">{label}</span>
                <span className="field-hint">{hint}</span>
              </label>
            ))}
          </div>
          <span className="field-hint">
            样片固定为竖版。拍组时 AI
            导演会为每张决定横竖：以竖版为主，交代环境的远景用横版；也可以在方案预览中逐张切换。只影响之后新生成的照片。
          </span>
        </div>
      )}

      <EmptyState icon="⚙" title="更多设置即将开放" description="并发数量、单价与提示词管理。" />
    </div>
  );
}

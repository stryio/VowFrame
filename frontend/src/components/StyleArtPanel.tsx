import { useCallback, useEffect, useState } from "react";
import { api, type StyleArt } from "../api";
import { useConfirm, useToast } from "./feedback";

const STATUS_TEXT: Record<NonNullable<StyleArt["status"]>, string> = {
  running: "生成中",
  ready: "可用",
  failed: "生成失败",
  interrupted: "生成中断",
};

/**
 * V7.33: every live style's art (its outfit worn by no one, standing in its main scene). New shots copy the clothes
 * and the place from it. It is made the first time a shot needs it; here it can be checked and made again.
 */
export function StyleArtPanel({ enabled, slotId }: { enabled: boolean; slotId?: string }) {
  const toast = useToast();
  const { confirm, dialog } = useConfirm();
  const [arts, setArts] = useState<StyleArt[] | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    (slotId ? api.slotStyleArts(slotId) : api.styleArts())
      .then((list) => {
        setArts(list);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, [slotId]);

  useEffect(load, [load]);

  const running = arts?.some((art) => art.status === "running") ?? false;
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [running, load]);

  const label = (art: StyleArt) => (art.room_id ? `${art.style_name} · ${art.room_name}` : art.style_name);

  const regenerate = async (art: StyleArt) => {
    const again = Boolean(art.in_use_id);
    const steps = art.room_id
      ? // V7.34: a room's scene is its main picture and the outfit picture is shared, so only the combination is paid.
        art.cost > 1
        ? "先做这套服饰的灰底服饰图，再把它放进场景主图里"
        : "把这套服饰的灰底服饰图放进场景主图里"
      : "灰底服饰图、空场景图，再把两者合成";
    const ok = await confirm({
      title: `${again ? "重新生成" : "现在生成"}「${label(art)}」的服饰场景图`,
      message: `将用默认生图模型做 ${art.cost} 次付费生成：${steps}。${again ? "新图生成好之前，新拍的照片继续用现在这张；已经拍好的照片不受影响。" : "做好后，在这个场景拍的照片都用它。"}`,
      confirmText: again ? "重新生成" : "现在生成",
    });
    if (!ok) return;
    try {
      await api.generateStyleArt(art.style_id, {
        room: art.room_id ? { id: art.room_id, area: 1 } : undefined,
        outfit: art.outfit_id,
      });
      toast.info("已开始生成，完成后自动显示");
      window.setTimeout(load, 500);
    } catch (err) {
      toast.error(err);
    }
  };

  if (failed) return <span className="field-hint">服饰场景图列表加载失败，请刷新页面重试。</span>;
  if (!arts) return <span className="field-hint">正在加载…</span>;

  return (
    <div className="style-art-grid" style={{ opacity: enabled ? 1 : 0.6 }}>
      {dialog}
      {arts.map((art) => {
        const replacing = art.in_use_id !== null && art.id !== art.in_use_id;
        return (
          <div key={`${art.style_id}/${art.room_id ?? ""}/${art.area ?? ""}`} className="style-art-item">
            <div className="style-art-main">
              {art.image_url ? (
                <a href={art.image_url} target="_blank" rel="noreferrer">
                  <img src={art.image_url} alt={`${label(art)}服饰场景图`} loading="lazy" />
                </a>
              ) : (
                <span className="field-hint">
                  {art.status === "running" ? "生成中…" : art.room_id ? "还没有生成，第一次在这个场景拍时自动生成" : "还没有生成，第一次拍这个风格时自动生成"}
                </span>
              )}
            </div>
            <div className="style-art-steps">
              {art.product_url && (
                <a href={art.product_url} target="_blank" rel="noreferrer" title="灰底服饰图">
                  <img src={art.product_url} alt="灰底服饰图" loading="lazy" />
                </a>
              )}
              {(art.scene_url ?? art.area_image_url) && (
                <a href={(art.scene_url ?? art.area_image_url)!} target="_blank" rel="noreferrer" title={art.room_id ? "场景主图" : "空场景图"}>
                  <img src={(art.scene_url ?? art.area_image_url)!} alt={art.room_id ? "场景主图" : "空场景图"} loading="lazy" />
                </a>
              )}
            </div>
            <div className="style-art-meta">
              {slotId ? (
                <span className="field-label">{art.room_name ?? art.style_name}</span>
              ) : (
                <span className="field-label">{art.room_id ? `${art.style_name} · ${art.room_name}` : art.style_name}</span>
              )}
              {!slotId && <span className="field-hint">{art.outfit_name}</span>}
              {art.status && (
                <span className={`field-hint ${art.status === "failed" || art.status === "interrupted" ? "text-danger" : ""}`}>
                  {replacing && art.status === "running" && "正在重新生成，暂时继续用当前这张"}
                  {replacing && art.status !== "running" && `最近一次${STATUS_TEXT[art.status]}，仍在用之前这张`}
                  {!replacing && STATUS_TEXT[art.status]}
                  {art.error_message && art.status !== "ready" && `：${art.error_message}`}
                </span>
              )}
              <button
                className="btn btn-ghost btn-sm"
                disabled={art.status === "running"}
                onClick={() => regenerate(art)}
              >
                {art.in_use_id ? "重新生成" : "现在生成"}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

import type { FaceScores } from "../api";

const HINT = "与上传正脸照的人脸相似度（FaceFusion 人脸识别），越接近 1 越像；未识别表示画面里没找到这个人的脸";

const value = (score: number | null) => (score === null ? "未识别" : score.toFixed(2));

/** "新娘 0.62 · 新郎 0.55", or null when the picture has not been measured. */
export function faceScoreText(scores?: FaceScores | null) {
  return scores ? `新娘 ${value(scores.bride)} · 新郎 ${value(scores.groom)}` : null;
}

export function FaceScore({ scores }: { scores?: FaceScores | null }) {
  const text = faceScoreText(scores);
  if (!text) return null;
  return (
    <span className="face-score" title={HINT}>
      相似度 {text}
    </span>
  );
}

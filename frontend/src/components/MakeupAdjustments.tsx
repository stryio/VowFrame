import { useEffect, useId, useState } from "react";
import type { MakeupLevel, RetouchSettings, Role } from "../api";

export const MAKEUP_PRESETS: Record<Role, [MakeupLevel, string][]> = {
  bride: [["light", "清透新娘妆"], ["natural", "标准婚纱妆"], ["refined", "精致影楼妆"]],
  groom: [["light", "清爽"], ["natural", "精修"], ["refined", "精致"]],
};

const ADJUSTMENT_OPTIONS = [
  ["off", "关闭"], ["minimal", "极轻"], ["subtle", "很轻"], ["light", "轻微"],
  ["mild", "偏轻"], ["balanced", "中等"], ["moderate", "适中"], ["enhanced", "偏强"],
  ["strong", "明显"], ["intense", "很强"], ["maximum", "最强"],
] as const;
const FIELDS = [
  { key: "slimming", label: "瘦脸程度", options: ADJUSTMENT_OPTIONS, hint: "只收整下颌两侧，保留颧骨、下巴特征与五官比例。" },
  { key: "brightening", label: "肤色提亮", options: ADJUSTMENT_OPTIONS.map(([value, label]) => [value, value === "off" ? "保持原肤色" : label] as const), hint: "调整肤色明度，保留原有冷暖底调。" },
  { key: "concealing", label: "遮瑕程度", options: ADJUSTMENT_OPTIONS.map(([value, label]) =>
    value === "balanced" ? ["standard", "标准"] as const : value === "maximum" ? ["full", "充分"] as const :
    [value, value === "light" ? "轻度" : label] as const), hint: "处理痘印与黑眼圈，保留皮肤纹理和辨识特征。" },
  { key: "chin", label: "下巴轮廓整理", options: ADJUSTMENT_OPTIONS, hint: "只整理明显的下巴下方软组织堆积，不收窄两侧。" },
  { key: "nasolabial", label: "法令纹淡化", options: ADJUSTMENT_OPTIONS.map(([value, label]) => [value, value === "off" ? "保留" : label] as const), hint: "淡化阴影与折痕，保留自然表情和年龄感。" },
] as const;

export function retouchSummary(settings: RetouchSettings): string {
  return FIELDS.map((field) => {
    const index = field.options.findIndex(([value]) => value === settings[field.key]);
    return `${field.label} ${index}/10 · ${field.options[index][1]}`;
  }).join(" · ");
}

export function makeupPresetLabel(role: Role, level: MakeupLevel): string {
  return MAKEUP_PRESETS[role].find(([value]) => value === level)![1];
}

function RetouchSlider({ id, field, settings, disabled, onChange }: {
  id: string;
  field: typeof FIELDS[number];
  settings: RetouchSettings;
  disabled: boolean;
  onChange: (settings: RetouchSettings) => void;
}) {
  const savedIndex = field.options.findIndex(([value]) => value === settings[field.key]);
  const [draftIndex, setDraftIndex] = useState(savedIndex);
  useEffect(() => {
    // Synchronize after a save, including a failed request that leaves the server value unchanged.
    if (!disabled) setDraftIndex(savedIndex);
  }, [savedIndex, disabled]);
  const commit = (input: HTMLInputElement) => {
    const index = Number(input.value);
    if (!disabled && index !== savedIndex) {
      onChange({ ...settings, [field.key]: field.options[index][0] });
    }
  };
  const valueText = `${draftIndex}/10 · ${field.options[draftIndex][1]}`;
  return (
    <div className="makeup-adjustment-field">
      <div className="makeup-adjustment-heading">
        <label htmlFor={id}>{field.label}</label>
        <output htmlFor={id}>{valueText}</output>
      </div>
      <input className="makeup-adjustment-slider" id={id} type="range" min={0} max={10} step={1}
        value={draftIndex} aria-valuetext={valueText} aria-describedby={`${id}-hint`}
        onChange={(event) => setDraftIndex(Number(event.currentTarget.value))}
        onPointerDown={(event) => event.currentTarget.setPointerCapture(event.pointerId)}
        onPointerUp={(event) => commit(event.currentTarget)}
        onPointerCancel={() => setDraftIndex(savedIndex)}
        onKeyUp={(event) => {
          if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
            commit(event.currentTarget);
          }
        }}
        onBlur={(event) => commit(event.currentTarget)} />
      <div className="makeup-adjustment-scale" aria-hidden="true"><span>0 · {field.options[0][1]}</span><span>10 · {field.options[10][1]}</span></div>
      <span id={`${id}-hint`} className="muted">{field.hint}</span>
    </div>
  );
}

export function MakeupAdjustments({ role, level, settings, disabled, saving, onChange, onPreset }: {
  role: Role;
  level: MakeupLevel;
  settings: RetouchSettings;
  disabled: boolean;
  saving: boolean;
  onChange: (settings: RetouchSettings) => void;
  onPreset: (level: MakeupLevel) => void;
}) {
  const id = useId();
  return (
    <details className="makeup-adjustments">
      <summary>精细调整 <span className="muted">· 瘦脸 {ADJUSTMENT_OPTIONS.findIndex(([value]) => value === settings.slimming)}/10</span></summary>
      <fieldset disabled={disabled} aria-busy={saving} aria-label={`${role === "bride" ? "新娘" : "新郎"}妆容与精细调整`}>
        <div className="makeup-adjustment-presets">
          <label htmlFor={`${id}-preset`}>快捷预设</label>
          <select className="input" id={`${id}-preset`} value={level} onChange={(event) => onPreset(event.target.value as MakeupLevel)}>
            {MAKEUP_PRESETS[role].map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <button className="btn btn-ghost btn-sm" onClick={() => onPreset(level)}>恢复本档预设</button>
          <span className="muted" role="status">{saving ? "保存中…" : "调整结束后自动保存"}</span>
        </div>
        <p className="muted">预设决定妆感并初始化下列选项；每项都可单独调整。{role === "bride" ? "新娘设置在各风格间共用，妆色与妆面仍按风格搭配。" : "新郎修脸照在所有风格间共用。"}</p>
        <div className="makeup-adjustment-grid">
          {FIELDS.map((field) => (
            <RetouchSlider key={field.key} id={`${id}-${field.key}`} field={field} settings={settings} disabled={disabled} onChange={onChange} />
          ))}
        </div>
        <p className="muted">每项 0～10 共 11 档，松手或键盘调整结束后保存。档位表示修图强度，不是脸宽变化百分比。改动只用于之后新生成的候选图，已有妆容、发型和成片保持原样。</p>
      </fieldset>
    </details>
  );
}

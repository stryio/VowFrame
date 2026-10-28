import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, assetUrl, type ProjectSummary, type Stage } from "../api";
import { EmptyState, useConfirm, useToast } from "../components/feedback";
import { useCurrentProject } from "../components/Layout";
import { usePolledResource } from "../hooks";

const STAGE_LABEL: Record<Stage, string> = {
  upload: "待上传照片",
  style: "试拍风格中",
  shooting: "拍摄中",
};

export function ProjectsPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const current = useCurrentProject();
  const { confirm, dialog } = useConfirm();
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<ProjectSummary | null>(null);
  const projects = usePolledResource(api.listProjects, [], (list) =>
    list.some((p) => p.active_task_count > 0),
  );

  const open = (p: ProjectSummary) => navigate(`/projects/${p.id}/workspace`);

  const create = async () => {
    setCreating(true);
    try {
      const p = await api.createProject();
      navigate(`/projects/${p.id}/workspace`);
    } catch (err) {
      toast.error(err);
      setCreating(false);
    }
  };

  const remove = async (p: ProjectSummary) => {
    const ok = await confirm({
      title: `删除「${p.name}」？`,
      message: "将删除这个项目的原始照片、所有生成的照片、历史版本和任务记录，且无法恢复。",
      confirmText: "删除项目",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteProject(p.id);
      toast.info("项目已删除");
      await projects.refresh();
      if (current.project?.id === p.id) await current.refresh();
    } catch (err) {
      toast.error(err);
    }
  };

  const rename = async (p: ProjectSummary, name: string) => {
    try {
      await api.updateProject(p.id, { name });
      setRenaming(null);
      await projects.refresh();
      if (current.project?.id === p.id) await current.refresh();
    } catch (err) {
      toast.error(err);
    }
  };

  const list = projects.data;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>我的项目</h1>
          <p>每个项目保存一对新人的照片、风格选择和所有生成结果。</p>
        </div>
        {list && list.length > 0 && (
          <button className="btn btn-primary" onClick={create} disabled={creating}>
            ＋ 新建项目
          </button>
        )}
      </div>

      {projects.error && !list ? (
        <EmptyState icon="!" title="无法连接本地服务" description={projects.error.message} />
      ) : !list ? (
        <div className="muted">加载中…</div>
      ) : list.length === 0 ? (
        <EmptyState
          icon="♡"
          title="还没有项目"
          description="上传两张人物照片，先试拍几种风格，再批量生成整套婚纱照。"
          action={
            <button className="btn btn-gold btn-lg" onClick={create} disabled={creating}>
              新建项目
            </button>
          }
        />
      ) : (
        <div className="project-grid">
          {list.map((p) => (
            <div key={p.id} className="card project-card" onClick={() => open(p)}>
              <div className="project-cover">
                {p.cover_asset_id ? <img src={assetUrl(p.cover_asset_id)} alt="" /> : "誓影"}
              </div>
              <div className="project-body">
                <h3>{p.name}</h3>
                <div className="project-meta">
                  <span>{new Date(p.created_at).toLocaleDateString("zh-CN")}</span>
                  <span>{p.photo_count} 张作品</span>
                  <span>{STAGE_LABEL[p.stage]}</span>
                </div>
                <div style={{ display: "flex", gap: 6, marginTop: 4, minHeight: 22 }}>
                  {p.active_task_count > 0 && (
                    <span className="tag tag-gold">生成中 {p.active_task_count}</span>
                  )}
                  {p.has_pending_recovery && <span className="tag tag-alert">有待恢复任务</span>}
                </div>
              </div>
              <div className="project-actions" onClick={(e) => e.stopPropagation()}>
                <button className="btn btn-ghost btn-sm" onClick={() => setRenaming(p)}>
                  重命名
                </button>
                <button className="btn btn-ghost btn-sm" onClick={() => remove(p)}>
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {renaming && (
        <RenameDialog
          initial={renaming.name}
          onCancel={() => setRenaming(null)}
          onSave={(name) => rename(renaming, name)}
        />
      )}
      {dialog}
    </div>
  );
}

function RenameDialog({
  initial,
  onSave,
  onCancel,
}: {
  initial: string;
  onSave: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial);
  const trimmed = name.trim();

  return (
    <div className="backdrop" onClick={onCancel}>
      <form
        className="card modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault();
          if (trimmed) onSave(trimmed);
        }}
      >
        <h2>重命名项目</h2>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={40} />
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={!trimmed}>
            保存
          </button>
        </div>
      </form>
    </div>
  );
}

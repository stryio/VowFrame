import { createContext, useContext, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useMatch } from "react-router-dom";
import { api, ApiError, isGenerating, type ProjectDetail } from "../api";
import { readCurrentProjectId, usePolledResource, writeCurrentProjectId } from "../hooks";
import { TaskDrawer } from "./TaskDrawer";

interface CurrentProject {
  project: ProjectDetail | null;
  /** Re-fetch now and keep polling while tasks are active. */
  refresh: () => Promise<void>;
  openTasks: () => void;
}

const CurrentProjectContext = createContext<CurrentProject | null>(null);

export function useCurrentProject() {
  const ctx = useContext(CurrentProjectContext);
  if (!ctx) throw new Error("useCurrentProject must be used inside Layout");
  return ctx;
}

const isBusy = (p: ProjectDetail) =>
  p.active_task_count > 0 || p.previews.some(isGenerating) || p.photos.some(isGenerating);

export function Layout() {
  const workspaceMatch = useMatch("/projects/:projectId/*");
  const routeProjectId = workspaceMatch?.params.projectId ?? null;
  const [projectId, setProjectId] = useState<string | null>(routeProjectId ?? readCurrentProjectId());
  const [drawerOpen, setDrawerOpen] = useState(false);
  const { pathname } = useLocation();
  const contentRef = useRef<HTMLElement>(null);

  // All pages share one scroll container; start each page at the top.
  useEffect(() => {
    contentRef.current?.scrollTo(0, 0);
  }, [pathname]);

  useEffect(() => {
    if (routeProjectId) {
      setProjectId(routeProjectId);
      writeCurrentProjectId(routeProjectId);
    }
  }, [routeProjectId]);

  const resource = usePolledResource(
    () => (projectId ? api.getProject(projectId) : Promise.resolve(null)),
    [projectId],
    (p) => p !== null && isBusy(p),
  );

  // A remembered project may have been deleted.
  useEffect(() => {
    if (resource.error instanceof ApiError && resource.error.status === 404) {
      writeCurrentProjectId(null);
      setProjectId(null);
    }
  }, [resource.error]);

  const project = resource.data ?? null;
  const ctx: CurrentProject = {
    project,
    refresh: resource.refresh,
    openTasks: () => setDrawerOpen(true),
  };

  return (
    <CurrentProjectContext.Provider value={ctx}>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-cn">誓影</div>
            <div className="brand-en">VowFrame</div>
          </div>
          <nav className="nav">
            <NavLink to="/projects" end className="nav-link">
              ◇ 项目
            </NavLink>
            {projectId ? (
              <>
                <NavLink to={`/projects/${projectId}/workspace`} className="nav-link">
                  ◎ 婚纱工作台
                </NavLink>
                <NavLink to={`/projects/${projectId}/album`} className="nav-link">
                  ▦ 婚纱相册
                </NavLink>
              </>
            ) : (
              <>
                <span className="nav-link disabled">◎ 婚纱工作台</span>
                <span className="nav-link disabled">▦ 婚纱相册</span>
              </>
            )}
          </nav>
          <div className="nav-spacer" />
          <nav className="nav">
            <NavLink to="/settings" className="nav-link">
              ⚙ 设置
            </NavLink>
          </nav>
        </aside>

        <div className="main">
          <header className="topbar">
            <div className="topbar-title">
              {project ? project.name : <span className="muted">未打开项目</span>}
            </div>
            <button className="icon-btn" onClick={() => setDrawerOpen(true)}>
              任务中心
              {project && project.active_task_count > 0 && (
                <span className="badge">{project.active_task_count}</span>
              )}
              {project?.has_pending_recovery && <span className="badge badge-alert">!</span>}
            </button>
          </header>
          <main className="content" ref={contentRef}>
            <Outlet />
          </main>
        </div>
      </div>

      {drawerOpen && (
        <TaskDrawer
          projectId={projectId}
          onClose={() => setDrawerOpen(false)}
          onChanged={resource.refresh}
        />
      )}
    </CurrentProjectContext.Provider>
  );
}

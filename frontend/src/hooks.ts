import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

export function useApi<T = any>(path: string | null, params?: Record<string, any>, deps: any[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const key = JSON.stringify(params || {});
  const reload = useCallback(() => {
    if (!path) { setData(null); return; }
    setLoading(true); setError(null);
    api<T>(path, { params }).then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, key, ...deps]);
  useEffect(() => { reload(); }, [reload]);
  return { data, error, loading, reload, setData };
}

export function useProject() {
  const [projectId, setProjectIdState] = useState<string | null>(() => { try { return localStorage.getItem("sdtf.project"); } catch { return null; } });
  const setProjectId = (id: string | null) => { setProjectIdState(id); try { if (id) localStorage.setItem("sdtf.project", id); else localStorage.removeItem("sdtf.project"); } catch {} window.dispatchEvent(new Event("sdtf.project")); };
  useEffect(() => { const h = () => { try { setProjectIdState(localStorage.getItem("sdtf.project")); } catch {} }; window.addEventListener("sdtf.project", h); return () => window.removeEventListener("sdtf.project", h); }, []);
  return { projectId, setProjectId };
}

export function useProjectDetails() {
  const { projectId } = useProject();
  const q = useApi<any>(projectId ? `/projects/${projectId}` : null);
  const source = q.data?.systems?.find((s: any) => s.role === "SOURCE");
  const target = q.data?.systems?.find((s: any) => s.role === "TARGET");
  return { projectId, project: q.data, source, target, reload: q.reload, error: q.error };
}

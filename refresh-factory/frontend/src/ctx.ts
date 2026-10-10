import { createContext, useContext } from "react";
import type { J } from "./api";

export interface Ctx {
  me: J; users: J[]; systems: J[]; projects: J[]; project: J | null; runId: string | null;
  reload: () => Promise<void>; selectProject: (id: string | null) => void; setRunId: (id: string | null) => void;
  go: (view: string) => void; can: (perm: string) => boolean;
}
export const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

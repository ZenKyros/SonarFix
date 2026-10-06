import { createContext, useCallback, useContext, useState } from "react";

const StatusContext = createContext(null);

let nextId = 1;

export function StatusProvider({ children }) {
  const [tasks, setTasks] = useState([]);

  const start = useCallback((label, detail) => {
    const id = nextId++;
    setTasks((prev) => [...prev, { id, label, detail: detail || "" }]);
    return id;
  }, []);

  const update = useCallback((id, detail) => {
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, detail } : t)));
  }, []);

  const finish = useCallback((id) => {
    setTasks((prev) => prev.filter((t) => t.id !== id));
  }, []);

  // Wrap an async action: shows `label` (and an optional live-updated detail)
  // in the status bar while it runs, and clears it as soon as it settles.
  const track = useCallback(
    async (label, fn, detail) => {
      const id = start(label, detail);
      try {
        return await fn((nextDetail) => update(id, nextDetail));
      } finally {
        finish(id);
      }
    },
    [start, update, finish]
  );

  return (
    <StatusContext.Provider value={{ tasks, track, start, update, finish }}>
      {children}
    </StatusContext.Provider>
  );
}

export function useStatus() {
  const ctx = useContext(StatusContext);
  if (!ctx) throw new Error("useStatus must be used inside StatusProvider");
  return ctx;
}

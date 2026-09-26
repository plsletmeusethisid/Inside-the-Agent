"use client";

import { useEffect, useState } from "react";

type Status = "checking" | "ready" | "offline";

export function ServiceStatus() {
  const [status, setStatus] = useState<Status>("checking");
  const [detail, setDetail] = useState("Checking API and vector database…");

  useEffect(() => {
    const controller = new AbortController();
    const baseUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
    fetch(`${baseUrl}/ready`, { signal: controller.signal, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Service is not ready");
        const data: { pgvector: string } = await response.json();
        setStatus("ready");
        setDetail(`API connected · PostgreSQL + pgvector ${data.pgvector}`);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setStatus("offline");
        setDetail(error instanceof Error ? error.message : "Connection failed");
      });
    return () => controller.abort();
  }, []);

  return (
    <div className={`service-status ${status}`} role="status" aria-live="polite">
      <span className="status-dot" />
      <span>{detail}</span>
    </div>
  );
}

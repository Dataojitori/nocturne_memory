import type {
  BrowseNodeResponse,
  DomainInfo,
  OrphanDetail,
  OrphanItem,
  PageKey,
  ResourceDiff,
  SessionInfo,
  SnapshotInfo,
  UpdateNodePayload,
} from "./types";

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

type ApiStatusListener = (online: boolean) => void;

const apiStatusListeners = new Set<ApiStatusListener>();
let apiOnline = true;

function setApiOnline(online: boolean) {
  if (apiOnline === online) return;
  apiOnline = online;
  for (const listener of apiStatusListeners) {
    listener(online);
  }
}

export function subscribeApiStatus(listener: ApiStatusListener): () => void {
  apiStatusListeners.add(listener);
  listener(apiOnline);
  return () => {
    apiStatusListeners.delete(listener);
  };
}

export async function probeApiHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${baseUrl}/health`);
    const online = response.ok;
    setApiOnline(online);
    return online;
  } catch {
    setApiOnline(false);
    return false;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      ...init,
    });
    setApiOnline(true);
  } catch (error) {
    setApiOnline(false);
    throw error;
  }

  if (!response.ok) {
    let detail = "Request failed";
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload?.detail) {
        detail = payload.detail;
      }
    } catch {
      detail = `Request failed: ${response.status}`;
    }
    throw new Error(detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

function encoded(value: string): string {
  return encodeURIComponent(value);
}

// ==================== Review ====================

export function getSessions(): Promise<SessionInfo[]> {
  return request<SessionInfo[]>("/review/sessions");
}

export function getSnapshots(sessionId: string): Promise<SnapshotInfo[]> {
  return request<SnapshotInfo[]>(`/review/sessions/${encoded(sessionId)}/snapshots`);
}

export function getResourceDiff(sessionId: string, resourceId: string): Promise<ResourceDiff> {
  return request<ResourceDiff>(`/review/sessions/${encoded(sessionId)}/diff/${encoded(resourceId)}`);
}

export function rollbackResource(sessionId: string, resourceId: string): Promise<unknown> {
  return request(`/review/sessions/${encoded(sessionId)}/rollback/${encoded(resourceId)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function approveSnapshot(sessionId: string, resourceId: string): Promise<unknown> {
  return request(`/review/sessions/${encoded(sessionId)}/snapshots/${encoded(resourceId)}`, {
    method: "DELETE",
  });
}

export function clearSession(sessionId: string): Promise<unknown> {
  return request(`/review/sessions/${encoded(sessionId)}`, {
    method: "DELETE",
  });
}

// ==================== Browse ====================

export function getDomains(): Promise<DomainInfo[]> {
  return request<DomainInfo[]>("/browse/domains");
}

export function getNode(domain: string, path: string): Promise<BrowseNodeResponse> {
  const query = new URLSearchParams({ domain, path });
  return request<BrowseNodeResponse>(`/browse/node?${query.toString()}`);
}

export function updateNode(domain: string, path: string, body: UpdateNodePayload): Promise<unknown> {
  const query = new URLSearchParams({ domain, path });
  return request(`/browse/node?${query.toString()}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ==================== Maintenance ====================

export function getOrphans(): Promise<OrphanItem[]> {
  return request<OrphanItem[]>("/maintenance/orphans");
}

export function getOrphanDetail(memoryId: number): Promise<OrphanDetail> {
  return request<OrphanDetail>(`/maintenance/orphans/${memoryId}`);
}

export function deleteOrphan(memoryId: number): Promise<unknown> {
  return request(`/maintenance/orphans/${memoryId}`, {
    method: "DELETE",
  });
}

// typed helper for nav
export const availablePages: PageKey[] = ["review", "memory", "cleanup"];

export type PageKey = "review" | "memory" | "cleanup";

// ==================== Review ====================

export interface SessionInfo {
  session_id: string;
  created_at?: string | null;
  resource_count: number;
}

export interface SnapshotInfo {
  resource_id: string;
  resource_type: string;
  snapshot_time: string;
  operation_type?: string;
  uri?: string | null;
}

export interface ResourceDiff {
  resource_id: string;
  resource_type: string;
  snapshot_time: string;
  snapshot_data: Record<string, unknown>;
  current_data: Record<string, unknown>;
  diff_unified: string;
  diff_summary: string;
  has_changes: boolean;
}

// ==================== Browse ====================

export interface DomainInfo {
  domain: string;
  root_count: number;
}

export interface BreadcrumbItem {
  path: string;
  label: string;
}

export interface BrowseNode {
  path: string;
  domain: string;
  uri: string;
  name: string;
  content: string;
  priority: number | null;
  disclosure: string | null;
  created_at: string | null;
  aliases: string[];
}

export interface BrowseChild {
  domain: string;
  path: string;
  uri: string;
  name: string;
  priority: number | null;
  disclosure: string | null;
  content_snippet: string;
}

export interface BrowseNodeResponse {
  node: BrowseNode;
  children: BrowseChild[];
  breadcrumbs: BreadcrumbItem[];
}

export interface UpdateNodePayload {
  content?: string;
  priority?: number;
  disclosure?: string;
}

// ==================== Maintenance ====================

export interface MigrationTargetPreview {
  id: number;
  paths: string[];
  content_snippet?: string;
}

export interface OrphanItem {
  id: number;
  content_snippet: string;
  created_at: string | null;
  deprecated: boolean;
  migrated_to: number | null;
  category: "deprecated" | "orphaned" | "active";
  migration_target: MigrationTargetPreview | null;
}

export interface MigrationTargetDetail {
  id: number;
  content: string;
  paths: string[];
  created_at: string | null;
}

export interface OrphanDetail {
  id: number;
  content: string;
  created_at: string | null;
  deprecated: boolean;
  migrated_to: number | null;
  category: "deprecated" | "orphaned" | "active";
  migration_target: MigrationTargetDetail | null;
}

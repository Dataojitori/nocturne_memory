import "./style.css";
import { html, render, type TemplateResult } from "lit";

import { Badge } from "@mariozechner/mini-lit/dist/Badge.js";
import { Button } from "@mariozechner/mini-lit/dist/Button.js";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@mariozechner/mini-lit/dist/Card.js";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
} from "@mariozechner/mini-lit/dist/Dialog.js";
import { Diff } from "@mariozechner/mini-lit/dist/Diff.js";
import { Select } from "@mariozechner/mini-lit/dist/Select.js";
import { createState } from "@mariozechner/mini-lit/dist/mini.js";

import {
  approveSnapshot,
  clearSession,
  deleteOrphan,
  getDomains,
  getNode,
  getOrphanDetail,
  getOrphans,
  getResourceDiff,
  getSessions,
  getSnapshots,
  probeApiHealth,
  rollbackResource,
  subscribeApiStatus,
  updateNode,
} from "./lib/api";
import type {
  BrowseNodeResponse,
  DomainInfo,
  OrphanDetail,
  OrphanItem,
  PageKey,
  ResourceDiff,
  SessionInfo,
  SnapshotInfo,
} from "./lib/types";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

interface ReviewState {
  refreshing: boolean;
  actionLoading: boolean;
  error: string | null;
  sessions: SessionInfo[];
  sessionId: string | null;
  snapshots: SnapshotInfo[];
  selectedResourceId: string | null;
  diffLoading: boolean;
  diffError: string | null;
  diff: ResourceDiff | null;
}

interface MemoryState {
  refreshing: boolean;
  saving: boolean;
  error: string | null;
  domains: DomainInfo[];
  domain: string;
  path: string;
  data: BrowseNodeResponse | null;
  editing: boolean;
  draftContent: string;
  draftPriority: number;
  draftDisclosure: string;
}

interface CleanupState {
  refreshing: boolean;
  deletingId: number | null;
  error: string | null;
  items: OrphanItem[];
  selectedId: number | null;
  detailLoading: boolean;
  detail: OrphanDetail | null;
  detailError: string | null;
}

interface ConfirmDialogState {
  isOpen: boolean;
  loading: boolean;
  title: string;
  description: string;
  confirmText: string;
  confirmVariant: "default" | "destructive";
  onConfirm: (() => Promise<void>) | null;
}

interface AppState {
  activePage: PageKey;
  apiOnline: boolean;
  review: ReviewState;
  memory: MemoryState;
  cleanup: CleanupState;
  confirmDialog: ConfirmDialogState;
}

const rootElement = document.querySelector("#app");
if (!(rootElement instanceof HTMLElement)) {
  throw new Error("#app container not found");
}

const pageMeta: Array<{ key: PageKey; label: string; subtitle: string }> = [
  { key: "review", label: "审查与回滚", subtitle: "审查并处理快照" },
  { key: "memory", label: "记忆浏览", subtitle: "浏览与编辑记忆树" },
  { key: "cleanup", label: "记忆清理", subtitle: "清理孤儿与废弃记忆" },
];

const reviewState = createState<ReviewState>({
  refreshing: false,
  actionLoading: false,
  error: null,
  sessions: [],
  sessionId: null,
  snapshots: [],
  selectedResourceId: null,
  diffLoading: false,
  diffError: null,
  diff: null,
});

const memoryState = createState<MemoryState>({
  refreshing: false,
  saving: false,
  error: null,
  domains: [],
  domain: "core",
  path: "",
  data: null,
  editing: false,
  draftContent: "",
  draftPriority: 0,
  draftDisclosure: "",
});

const cleanupState = createState<CleanupState>({
  refreshing: false,
  deletingId: null,
  error: null,
  items: [],
  selectedId: null,
  detailLoading: false,
  detail: null,
  detailError: null,
});

const confirmDialogState = createState<ConfirmDialogState>({
  isOpen: false,
  loading: false,
  title: "",
  description: "",
  confirmText: "确认",
  confirmVariant: "default",
  onConfirm: null,
});

const state = createState<AppState>({
  activePage: "review",
  apiOnline: true,
  review: reviewState,
  memory: memoryState,
  cleanup: cleanupState,
  confirmDialog: confirmDialogState,
});

let reviewRequestToken = 0;
let diffRequestToken = 0;
let memoryRequestToken = 0;
let cleanupRequestToken = 0;
let cleanupDetailToken = 0;

const reviewDiffCache = new Map<string, ResourceDiff>();
const minButtonLoadingMs = 300;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function withMinButtonLoading(task: () => Promise<void>, setLoading: (value: boolean) => void) {
  const startAt = Date.now();
  setLoading(true);
  try {
    await task();
  } finally {
    const remain = minButtonLoadingMs - (Date.now() - startAt);
    if (remain > 0) {
      await sleep(remain);
    }
    setLoading(false);
  }
}

function fmtTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function switchPage(page: PageKey) {
  state.activePage = page;
}

function openConfirmDialog(options: {
  title: string;
  description: string;
  confirmText: string;
  confirmVariant?: "default" | "destructive";
  onConfirm: () => Promise<void>;
}) {
  state.confirmDialog.title = options.title;
  state.confirmDialog.description = options.description;
  state.confirmDialog.confirmText = options.confirmText;
  state.confirmDialog.confirmVariant = options.confirmVariant ?? "default";
  state.confirmDialog.onConfirm = options.onConfirm;
  state.confirmDialog.loading = false;
  state.confirmDialog.isOpen = true;
}

function closeConfirmDialog() {
  if (state.confirmDialog.loading) return;
  state.confirmDialog.isOpen = false;
  state.confirmDialog.onConfirm = null;
}

async function submitConfirmDialog() {
  const onConfirm = state.confirmDialog.onConfirm;
  if (!onConfirm || state.confirmDialog.loading) return;

  state.confirmDialog.loading = true;
  try {
    await onConfirm();
    state.confirmDialog.isOpen = false;
    state.confirmDialog.onConfirm = null;
  } finally {
    state.confirmDialog.loading = false;
  }
}

function selectedSnapshot(): SnapshotInfo | null {
  return (
    state.review.snapshots.find((item) => item.resource_id === state.review.selectedResourceId) ?? null
  );
}

function selectedCleanupItem(): OrphanItem | null {
  return state.cleanup.items.find((item) => item.id === state.cleanup.selectedId) ?? null;
}

function statusBadgeVariantByOperation(operationType?: string): BadgeVariant {
  switch (operationType) {
    case "delete":
      return "destructive";
    case "create":
    case "create_alias":
      return "default";
    case "modify_meta":
      return "secondary";
    default:
      return "outline";
  }
}

function cleanupBadgeVariant(category: OrphanItem["category"]): BadgeVariant {
  if (category === "deprecated") return "secondary";
  if (category === "orphaned") return "destructive";
  return "outline";
}

function formatOperationLabel(operationType?: string): string {
  if (operationType === "create") return "创建";
  if (operationType === "create_alias") return "创建别名";
  if (operationType === "delete") return "删除";
  if (operationType === "modify_meta") return "修改元数据";
  if (operationType === "modify_content") return "修改内容";
  if (operationType === "modify") return "修改";
  return "修改";
}

function formatResourceType(resourceType?: string): string {
  if (resourceType === "path") return "路径";
  if (resourceType === "memory") return "记忆";
  return resourceType ?? "未知";
}

function formatPageLabel(page: PageKey): string {
  if (page === "review") return "审查";
  if (page === "memory") return "浏览";
  return "清理";
}

function formatApiStatusLabel(online: boolean): string {
  return online ? "在线" : "离线";
}

function formatCleanupCategory(category: OrphanItem["category"]): string {
  if (category === "deprecated") return "废弃";
  if (category === "orphaned") return "孤儿";
  return "活动";
}

function isCreateOperation(operationType?: string): boolean {
  return operationType === "create" || operationType === "create_alias";
}

async function loadReviewDiff(sessionId: string, resourceId: string) {
  const token = ++diffRequestToken;
  const cacheKey = `${sessionId}::${resourceId}`;
  const cached = reviewDiffCache.get(cacheKey);

  state.review.diffError = null;

  if (cached) {
    state.review.diff = cached;
    state.review.diffLoading = false;
  } else {
    state.review.diffLoading = state.review.diff === null;
  }

  try {
    const diff = await getResourceDiff(sessionId, resourceId);
    if (token !== diffRequestToken) return;
    reviewDiffCache.set(cacheKey, diff);
    state.review.diff = diff;
  } catch (error) {
    if (token !== diffRequestToken) return;
    state.review.diffError = error instanceof Error ? error.message : "无法获取差异";
  } finally {
    if (token === diffRequestToken) {
      state.review.diffLoading = false;
    }
  }
}

async function loadReviewSnapshots(sessionId: string) {
  state.review.error = null;
  const currentSelected = state.review.selectedResourceId;

  try {
    const snapshots = await getSnapshots(sessionId);
    state.review.snapshots = snapshots;

    if (snapshots.length === 0) {
      state.review.selectedResourceId = null;
      state.review.diff = null;
      state.review.diffError = null;
      return;
    }

    const nextSelected =
      currentSelected && snapshots.some((item) => item.resource_id === currentSelected)
        ? currentSelected
        : snapshots[0].resource_id;

    const needReloadDiff = nextSelected !== state.review.selectedResourceId || !state.review.diff;
    state.review.selectedResourceId = nextSelected;

    if (needReloadDiff) {
      await loadReviewDiff(sessionId, nextSelected);
    }
  } catch (error) {
    state.review.error = error instanceof Error ? error.message : "加载快照失败";
  }
}

async function reloadReview() {
  const token = ++reviewRequestToken;
  state.review.error = null;

  try {
    const sessions = await getSessions();
    if (token !== reviewRequestToken) return;

    state.review.sessions = sessions;

    if (sessions.length === 0) {
      state.review.sessionId = null;
      state.review.snapshots = [];
      state.review.selectedResourceId = null;
      state.review.diff = null;
      return;
    }

    const preferred =
      state.review.sessionId && sessions.some((item) => item.session_id === state.review.sessionId)
        ? state.review.sessionId
        : sessions[0].session_id;

    state.review.sessionId = preferred;
    await loadReviewSnapshots(preferred);
  } catch (error) {
    if (token !== reviewRequestToken) return;
    state.review.error = error instanceof Error ? error.message : "加载会话失败";
  }
}

async function handleSessionChange(sessionId: string) {
  state.review.sessionId = sessionId;
  await loadReviewSnapshots(sessionId);
}

async function handleSnapshotSelect(resourceId: string) {
  if (!state.review.sessionId) return;
  if (state.review.selectedResourceId === resourceId) return;
  state.review.selectedResourceId = resourceId;
  await loadReviewDiff(state.review.sessionId, resourceId);
}

async function handleApproveSnapshot() {
  const sessionId = state.review.sessionId;
  const resourceId = state.review.selectedResourceId;
  if (!sessionId || !resourceId) return;

  state.review.actionLoading = true;
  try {
    await approveSnapshot(sessionId, resourceId);
    await reloadReview();
  } catch (error) {
    state.review.error = error instanceof Error ? error.message : "审批失败";
  } finally {
    state.review.actionLoading = false;
  }
}

async function handleRejectSnapshot() {
  const sessionId = state.review.sessionId;
  const resourceId = state.review.selectedResourceId;
  if (!sessionId || !resourceId) return;

  state.review.actionLoading = true;
  try {
    await rollbackResource(sessionId, resourceId);
    await approveSnapshot(sessionId, resourceId);
    await reloadReview();
  } catch (error) {
    state.review.error = error instanceof Error ? error.message : "回滚失败";
  } finally {
    state.review.actionLoading = false;
  }
}

async function handleClearSession() {
  const sessionId = state.review.sessionId;
  if (!sessionId) return;

  state.review.actionLoading = true;
  try {
    await clearSession(sessionId);
    await reloadReview();
  } catch (error) {
    state.review.error = error instanceof Error ? error.message : "全部通过失败";
  } finally {
    state.review.actionLoading = false;
  }
}

async function handleReviewRefresh() {
  if (state.review.refreshing || state.review.actionLoading) return;
  await withMinButtonLoading(
    async () => {
      await reloadReview();
    },
    (value) => {
      state.review.refreshing = value;
    },
  );
}

async function handleMemoryRefresh() {
  if (state.memory.refreshing) return;
  await withMinButtonLoading(
    async () => {
      await loadDomainsAndNode();
    },
    (value) => {
      state.memory.refreshing = value;
    },
  );
}

async function handleCleanupRefresh() {
  if (state.cleanup.refreshing) return;
  await withMinButtonLoading(
    async () => {
      await reloadCleanup();
    },
    (value) => {
      state.cleanup.refreshing = value;
    },
  );
}

async function loadDomainsAndNode(options: { withDomains?: boolean } = {}) {
  const { withDomains = true } = options;
  state.memory.error = null;
  const token = ++memoryRequestToken;

  try {
    if (withDomains) {
      const domains = await getDomains();
      if (token !== memoryRequestToken) return;

      state.memory.domains = domains;

      if (domains.length > 0 && !domains.some((item) => item.domain === state.memory.domain)) {
        state.memory.domain = domains[0].domain;
        state.memory.path = "";
      }
    }

    const node = await getNode(state.memory.domain, state.memory.path);
    if (token !== memoryRequestToken) return;

    state.memory.data = node;
    state.memory.editing = false;
    state.memory.draftContent = node.node.content ?? "";
    state.memory.draftPriority = node.node.priority ?? 0;
    state.memory.draftDisclosure = node.node.disclosure ?? "";
  } catch (error) {
    if (token !== memoryRequestToken) return;
    state.memory.error = error instanceof Error ? error.message : "加载记忆节点失败";
  }
}

async function navigateMemory(domain: string, path: string) {
  state.memory.domain = domain;
  state.memory.path = path;
  await loadDomainsAndNode({ withDomains: false });
}

function startEditNode() {
  const node = state.memory.data?.node;
  if (!node || state.memory.path === "") return;

  state.memory.editing = true;
  state.memory.draftContent = node.content ?? "";
  state.memory.draftPriority = node.priority ?? 0;
  state.memory.draftDisclosure = node.disclosure ?? "";
}

function cancelEditNode() {
  const node = state.memory.data?.node;
  if (!node) return;

  state.memory.editing = false;
  state.memory.draftContent = node.content ?? "";
  state.memory.draftPriority = node.priority ?? 0;
  state.memory.draftDisclosure = node.disclosure ?? "";
}

async function saveNode() {
  const node = state.memory.data?.node;
  if (!node || state.memory.path === "") return;

  const payload: { content?: string; priority?: number; disclosure?: string } = {};

  if (state.memory.draftContent !== (node.content ?? "")) {
    payload.content = state.memory.draftContent;
  }
  if (state.memory.draftPriority !== (node.priority ?? 0)) {
    payload.priority = state.memory.draftPriority;
  }
  if (state.memory.draftDisclosure !== (node.disclosure ?? "")) {
    payload.disclosure = state.memory.draftDisclosure;
  }

  if (Object.keys(payload).length === 0) {
    state.memory.editing = false;
    return;
  }

  state.memory.saving = true;
  state.memory.error = null;

  try {
    await updateNode(state.memory.domain, state.memory.path, payload);
    await loadDomainsAndNode({ withDomains: false });
  } catch (error) {
    state.memory.error = error instanceof Error ? error.message : "保存失败";
  } finally {
    state.memory.saving = false;
  }
}

async function reloadCleanup() {
  const token = ++cleanupRequestToken;
  state.cleanup.error = null;

  try {
    const items = await getOrphans();
    if (token !== cleanupRequestToken) return;

    state.cleanup.items = items;

    if (items.length === 0) {
      state.cleanup.selectedId = null;
      state.cleanup.detail = null;
      state.cleanup.detailError = null;
      return;
    }

    const preferred =
      state.cleanup.selectedId && items.some((item) => item.id === state.cleanup.selectedId)
        ? state.cleanup.selectedId
        : items[0].id;

    await selectCleanupItem(preferred);
  } catch (error) {
    if (token !== cleanupRequestToken) return;
    state.cleanup.error = error instanceof Error ? error.message : "加载 orphan 列表失败";
  }
}

async function selectCleanupItem(memoryId: number) {
  const token = ++cleanupDetailToken;
  state.cleanup.selectedId = memoryId;
  state.cleanup.detailLoading = true;
  state.cleanup.detailError = null;

  try {
    const detail = await getOrphanDetail(memoryId);
    if (token !== cleanupDetailToken) return;
    state.cleanup.detail = detail;
  } catch (error) {
    if (token !== cleanupDetailToken) return;
    state.cleanup.detail = null;
    state.cleanup.detailError = error instanceof Error ? error.message : "加载详情失败";
  } finally {
    if (token === cleanupDetailToken) {
      state.cleanup.detailLoading = false;
    }
  }
}

async function handleDeleteOrphan(memoryId: number) {
  state.cleanup.deletingId = memoryId;

  try {
    await deleteOrphan(memoryId);
    await reloadCleanup();
  } catch (error) {
    state.cleanup.error = error instanceof Error ? error.message : "删除失败";
  } finally {
    state.cleanup.deletingId = null;
  }
}

function getTextValue(input: Record<string, unknown>, key: string): string {
  const value = input[key];
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value == null) return "";
  return JSON.stringify(value, null, 2);
}

function renderReviewPage(): TemplateResult {
  const snapshot = selectedSnapshot();

  return html`
    <div class="space-y-5">
      ${Card(html`
        ${CardHeader(html`
          ${CardTitle("审查会话")}
          ${CardDescription("查看快照队列，逐条通过或回滚")}
        `)}
        ${CardContent(html`
          <div class="grid gap-4 lg:grid-cols-[minmax(0,280px)_1fr_auto] lg:items-end">
            <div class="space-y-1 text-sm">
              <span class="text-xs text-muted-foreground">会话</span>
              ${Select({
                value: state.review.sessionId ?? "",
                placeholder: "暂无会话",
                options: state.review.sessions.map((item) => ({
                  value: item.session_id,
                  label: `${item.session_id} (${item.resource_count})`,
                })),
                onChange: (value) => {
                  if (value) void handleSessionChange(value);
                },
                disabled: state.review.sessions.length === 0,
                width: "100%",
                variant: "outline",
              })}
            </div>
            <div class="text-xs text-muted-foreground">
              共 ${state.review.sessions.length} 个 session，当前快照 ${state.review.snapshots.length} 条
            </div>
            <div class="flex gap-2">
              ${Button({
                variant: "outline",
                loading: state.review.refreshing,
                disabled: state.review.actionLoading || state.review.refreshing,
                children: "刷新",
                onClick: () => {
                  void handleReviewRefresh();
                },
              })}
              ${Button({
                variant: "secondary",
                disabled:
                  !state.review.sessionId ||
                  state.review.snapshots.length === 0 ||
                  state.review.actionLoading,
                children: "通过全部",
                onClick: () => {
                  openConfirmDialog({
                    title: "通过全部",
                    description: "确认通过当前会话的全部待审快照？",
                    confirmText: "确认通过",
                    onConfirm: handleClearSession,
                  });
                },
              })}
            </div>
          </div>
        `)}
      `, true)}

      ${state.review.error
        ? Card(html`
            ${CardHeader(html`${CardTitle("请求失败")} ${CardDescription(state.review.error)}`)}
          `)
        : ""}

      <div class="grid gap-4 lg:grid-cols-[minmax(0,35%)_minmax(0,65%)]">
        ${Card(html`
          ${CardHeader(html`${CardTitle("快照列表")} ${CardDescription("选择一个快照查看差异")}`)}
          ${CardContent(html`
            <div class="space-y-2">
              ${state.review.snapshots.length === 0
                ? html`<p class="text-sm text-muted-foreground">暂无待审快照</p>`
                : state.review.snapshots.map(
                    (item) => html`
                      <button
                        type="button"
                        class="w-full cursor-pointer rounded-md border px-3.5 py-2.5 text-left transition-colors ${item.resource_id ===
                        state.review.selectedResourceId
                          ? "border-primary/20 bg-primary/5"
                          : "border-border/70 bg-background/85 hover:bg-muted/40"}"
                        @click=${(event: Event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          void handleSnapshotSelect(item.resource_id);
                        }}
                      >
                        <div class="mb-1 flex items-center justify-between gap-2">
                          <p class="truncate text-sm font-medium">${item.uri ?? item.resource_id}</p>
                          ${Badge(formatResourceType(item.resource_type), "outline")}
                        </div>
                        <div class="flex items-center gap-2 text-xs text-muted-foreground">
                          ${Badge(formatOperationLabel(item.operation_type), statusBadgeVariantByOperation(item.operation_type))}
                          <span>${fmtTime(item.snapshot_time)}</span>
                        </div>
                      </button>
                    `,
                  )}
            </div>
          `)}
        `)}

        ${Card(html`
          ${CardHeader(html`
            ${CardTitle(snapshot ? snapshot.uri ?? snapshot.resource_id : "差异详情")}
            ${CardDescription(snapshot ? `快照时间：${fmtTime(snapshot.snapshot_time)}` : "请选择左侧快照")}
          `)}
          ${CardContent(html`
            ${state.review.diffError
              ? html`<p class="text-sm text-destructive">${state.review.diffError}</p>`
              : state.review.diff
                ? html`
                    <div class="space-y-3">
                      ${isCreateOperation(snapshot?.operation_type)
                        ? html`
                            <div class="flex flex-wrap items-center gap-2">
                              ${Badge("创建快照", "secondary")}
                              ${Badge(formatOperationLabel(snapshot?.operation_type), "outline")}
                            </div>

                            <section class="rounded-md border border-border/70 bg-background/85 p-3.5">
                              <p class="mb-2 text-xs font-medium text-muted-foreground">元数据</p>
                              <div class="grid gap-2 text-xs sm:grid-cols-[88px_1fr]">
                                <span class="text-muted-foreground">优先级</span>
                                <span class="font-mono">${getTextValue(state.review.diff.current_data, "priority") || "（无）"}</span>
                                <span class="text-muted-foreground">触发条件</span>
                                <span>${getTextValue(state.review.diff.current_data, "disclosure") || "（无）"}</span>
                              </div>
                            </section>

                            <section class="rounded-md border border-border/70 bg-background/85 p-3.5">
                              <p class="mb-2 text-xs font-medium text-muted-foreground">创建内容预览</p>
                              <pre class="max-h-72 overflow-auto whitespace-pre-wrap text-xs leading-5">${getTextValue(
                                state.review.diff.current_data,
                                "content",
                              ) || "（空）"}</pre>
                            </section>
                          `
                        : html`
                            <div class="flex flex-wrap items-center gap-2">
                              ${Badge(state.review.diff.has_changes ? "内容有变化" : "内容无变化", state.review.diff.has_changes
                                ? "secondary"
                                : "outline")}
                              <span class="text-xs text-muted-foreground">${state.review.diff.diff_summary}</span>
                            </div>

                            ${Diff({
                              oldText: getTextValue(state.review.diff.snapshot_data, "content"),
                              newText: getTextValue(state.review.diff.current_data, "content"),
                              title: "内容差异查看",
                            })}
                          `}
                    </div>
                  `
                : state.review.diffLoading
                  ? html`<p class="text-sm text-muted-foreground">正在加载差异...</p>`
                  : html`<p class="text-sm text-muted-foreground">暂无差异数据</p>`}
          `)}
          ${CardFooter(html`
            <div class="flex flex-wrap items-center gap-2">
              ${Button({
                variant: "default",
                disabled: !snapshot || state.review.actionLoading,
                children: "通过",
                onClick: () => {
                  void handleApproveSnapshot();
                },
              })}
              ${Button({
                variant: "destructive",
                disabled: !snapshot || state.review.actionLoading,
                children: "回滚并拒绝",
                onClick: () => {
                  const resourceId = state.review.selectedResourceId;
                  if (!resourceId) return;
                  openConfirmDialog({
                    title: "回滚并拒绝",
                    description: `确认回滚并拒绝：${resourceId} ？`,
                    confirmText: "确认执行",
                    confirmVariant: "destructive",
                    onConfirm: handleRejectSnapshot,
                  });
                },
              })}
            </div>
          `)}
        `, true)}
      </div>
    </div>
  `;
}

function renderMemoryPage(): TemplateResult {
  const memoryNode = state.memory.data?.node;
  const children = state.memory.data?.children ?? [];
  const breadcrumbs = state.memory.data?.breadcrumbs ?? [];
  const atRoot = state.memory.path.length === 0;

  return html`
    <div class="space-y-5">
      ${Card(html`
        ${CardHeader(html`${CardTitle("记忆浏览")}
        ${CardDescription("浏览路径、查看别名、编辑内容与元数据")}`)}
        ${CardContent(html`
          <div class="grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)_auto] lg:items-end">
            <div class="space-y-1 text-sm">
              <span class="text-xs text-muted-foreground">领域</span>
              ${Select({
                value: state.memory.domain,
                placeholder: "选择领域",
                options:
                  state.memory.domains.length === 0
                    ? [{ value: "core", label: "core" }]
                    : state.memory.domains.map((item) => ({ value: item.domain, label: item.domain })),
                onChange: (value) => {
                  void navigateMemory(value, "");
                },
                width: "100%",
                variant: "outline",
              })}
            </div>
            <div class="space-y-1 text-sm">
              <span class="text-xs text-muted-foreground">当前路径</span>
              <p class="rounded-md border border-border/70 bg-background/85 px-3 py-2 font-mono text-xs">
                ${state.memory.domain}://${state.memory.path || "root"}
              </p>
            </div>
            <div class="flex gap-2">
              ${Button({
                variant: "outline",
                loading: state.memory.refreshing,
                disabled: state.memory.refreshing,
                children: "刷新",
                onClick: () => {
                  void handleMemoryRefresh();
                },
              })}
              ${Button({
                variant: state.memory.editing ? "secondary" : "default",
                disabled: atRoot,
                children: state.memory.editing ? "编辑中" : "编辑节点",
                onClick: () => {
                  startEditNode();
                },
              })}
            </div>
          </div>
        `)}
      `, true)}

      ${state.memory.error
        ? Card(html`${CardHeader(html`${CardTitle("请求失败")} ${CardDescription(state.memory.error)}`)}`)
        : ""}

      <div class="grid gap-4 lg:grid-cols-[minmax(0,35%)_minmax(0,65%)]">
        ${Card(html`
          ${CardHeader(html`${CardTitle("子节点")}
          ${CardDescription("点击子节点进入下一层")}`)}
          ${CardContent(html`
            <div class="mb-3 flex flex-wrap items-center gap-2 text-xs">
              ${breadcrumbs.map(
                (item, index) => html`
                  <button
                    type="button"
                    class="cursor-pointer rounded border border-border/70 px-2 py-1 ${index === breadcrumbs.length - 1
                      ? "bg-primary/5"
                      : "bg-background hover:bg-muted/40"}"
                    @click=${(event: Event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      void navigateMemory(state.memory.domain, item.path);
                    }}
                  >
                    ${item.label}
                  </button>
                `,
              )}
            </div>

            ${children.length === 0
              ? html`<p class="text-sm text-muted-foreground">没有子节点</p>`
              : html`
                  <div class="space-y-2">
                    ${children.map(
                      (child) => html`
                        <button
                          type="button"
                          class="w-full cursor-pointer rounded-md border border-border/70 bg-background/85 px-3.5 py-2.5 text-left hover:bg-muted/40"
                          @click=${(event: Event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            void navigateMemory(child.domain, child.path);
                          }}
                        >
                          <div class="mb-1 flex items-center justify-between gap-2">
                            <p class="truncate text-sm font-medium">${child.name}</p>
                            ${Badge(`P${child.priority ?? "-"}`, "outline")}
                          </div>
                          <p class="truncate text-xs text-muted-foreground">${child.domain}://${child.path}</p>
                          <p class="mt-1 text-xs text-muted-foreground line-clamp-2">${child.content_snippet || "（空）"}</p>
                        </button>
                      `,
                    )}
                  </div>
                `}
          `)}
        `)}

        ${Card(html`
          ${CardHeader(html`${CardTitle(memoryNode?.uri ?? "节点详情")}
          ${CardDescription("查看或编辑内容、优先级、触发条件")}`)}
          ${CardContent(html`
            ${!memoryNode
              ? html`<p class="text-sm text-muted-foreground">暂无节点数据</p>`
              : html`
                  <div class="space-y-4">
                    <div class="grid gap-3 sm:grid-cols-3">
                      <div class="rounded-md border border-border/70 bg-background/85 p-3.5">
                        <p class="text-xs text-muted-foreground">优先级</p>
                        <p class="text-lg font-semibold">${memoryNode.priority ?? "-"}</p>
                      </div>
                      <div class="rounded-md border border-border/70 bg-background/85 p-3.5 sm:col-span-2">
                        <p class="text-xs text-muted-foreground">触发条件</p>
                        <p class="text-sm">${memoryNode.disclosure || "（无）"}</p>
                      </div>
                    </div>

                    ${memoryNode.aliases?.length
                      ? html`
                          <div class="rounded-md border border-border/70 bg-background/85 p-3.5">
                            <p class="mb-2 text-xs text-muted-foreground">别名</p>
                            <div class="space-y-1">
                              ${memoryNode.aliases.map((alias) => html`<p class="font-mono text-xs">${alias}</p>`)}
                            </div>
                          </div>
                        `
                      : ""}

                    ${state.memory.editing
                      ? html`
                          <div class="space-y-3 rounded-md border border-border/70 bg-background/85 p-3.5">
                            <label class="space-y-1 text-sm">
                              <span class="text-xs text-muted-foreground">优先级</span>
                              <input
                                type="number"
                                min="0"
                                class="w-full rounded-md border border-border/70 bg-background px-3 py-2 text-sm"
                                .value=${String(state.memory.draftPriority)}
                                @input=${(event: Event) => {
                                  const value = Number((event.target as HTMLInputElement).value);
                                  state.memory.draftPriority = Number.isFinite(value) ? Math.max(0, value) : 0;
                                }}
                              />
                            </label>

                            <label class="space-y-1 text-sm">
                              <span class="text-xs text-muted-foreground">触发条件</span>
                              <input
                                type="text"
                                class="w-full rounded-md border border-border/70 bg-background px-3 py-2 text-sm"
                                .value=${state.memory.draftDisclosure}
                                @input=${(event: Event) => {
                                  state.memory.draftDisclosure = (event.target as HTMLInputElement).value;
                                }}
                              />
                            </label>

                            <label class="space-y-1 text-sm">
                              <span class="text-xs text-muted-foreground">内容</span>
                              <textarea
                                class="min-h-65 w-full rounded-md border border-border/70 bg-background px-3 py-2 font-mono text-sm"
                                .value=${state.memory.draftContent}
                                @input=${(event: Event) => {
                                  state.memory.draftContent = (event.target as HTMLTextAreaElement).value;
                                }}
                              ></textarea>
                            </label>
                          </div>
                        `
                      : html`
                          <div class="rounded-md border border-border/70 bg-background/85 p-3.5">
                            <p class="mb-2 text-xs text-muted-foreground">内容</p>
                            <pre class="max-h-90 overflow-auto whitespace-pre-wrap text-xs leading-5">${memoryNode.content}</pre>
                          </div>
                        `}
                  </div>
                `}
          `)}
          ${CardFooter(html`
            <div class="flex flex-wrap gap-2">
              ${state.memory.editing
                ? html`
                    ${Button({
                      variant: "default",
                      loading: state.memory.saving,
                      children: "保存",
                      onClick: () => {
                        void saveNode();
                      },
                    })}
                    ${Button({
                      variant: "outline",
                      disabled: state.memory.saving,
                      children: "取消",
                      onClick: () => {
                        cancelEditNode();
                      },
                    })}
                  `
                : ""}
            </div>
          `)}
        `, true)}
      </div>
    </div>
  `;
}

function renderCleanupPage(): TemplateResult {
  const selected = selectedCleanupItem();

  return html`
    <div class="space-y-5">
      ${Card(html`
        ${CardHeader(html`${CardTitle("记忆清理")}
        ${CardDescription("检查废弃与孤儿记忆，并执行永久删除")}`)}
        ${CardContent(html`
          <div class="grid gap-4 lg:grid-cols-3">
            <div class="rounded-md border border-border/70 bg-background/85 p-3.5">
              <p class="text-xs text-muted-foreground">总数</p>
              <p class="text-3xl font-semibold">${state.cleanup.items.length}</p>
            </div>
            <div class="rounded-md border border-border/70 bg-background/85 p-3.5">
              <p class="text-xs text-muted-foreground">废弃</p>
              <p class="text-3xl font-semibold">${state.cleanup.items.filter((item) => item.category === "deprecated").length}</p>
            </div>
            <div class="rounded-md border border-border/70 bg-background/85 p-3.5">
              <p class="text-xs text-muted-foreground">孤儿</p>
              <p class="text-3xl font-semibold">${state.cleanup.items.filter((item) => item.category === "orphaned").length}</p>
            </div>
          </div>
        `)}
        ${CardFooter(html`
          ${Button({
            variant: "outline",
            loading: state.cleanup.refreshing,
            disabled: state.cleanup.refreshing,
            children: "刷新列表",
            onClick: () => {
              void handleCleanupRefresh();
            },
          })}
        `)}
      `, true)}

      ${state.cleanup.error
        ? Card(html`${CardHeader(html`${CardTitle("请求失败")} ${CardDescription(state.cleanup.error)}`)}`)
        : ""}

      <div class="grid gap-4 lg:grid-cols-[minmax(0,35%)_minmax(0,65%)]">
        ${Card(html`
          ${CardHeader(html`${CardTitle("清理列表")}
          ${CardDescription("点击条目加载详情")}`)}
          ${CardContent(html`
            ${state.cleanup.items.length === 0
              ? html`<p class="text-sm text-muted-foreground">暂无可清理项</p>`
              : html`
                  <div class="space-y-2">
                    ${state.cleanup.items.map(
                      (item) => html`
                        <button
                          type="button"
                          class="w-full cursor-pointer rounded-md border px-3.5 py-2.5 text-left transition-colors ${item.id ===
                          state.cleanup.selectedId
                            ? "border-primary/20 bg-primary/5"
                            : "border-border/70 bg-background/85 hover:bg-muted/40"}"
                          @click=${(event: Event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            void selectCleanupItem(item.id);
                          }}
                        >
                          <div class="mb-1 flex items-center justify-between gap-2">
                            <p class="text-sm font-medium">记忆 #${item.id}</p>
                            ${Badge(formatCleanupCategory(item.category), cleanupBadgeVariant(item.category))}
                          </div>
                          <p class="truncate text-xs text-muted-foreground">${fmtTime(item.created_at)}</p>
                          <p class="mt-1 text-xs text-muted-foreground line-clamp-2">${item.content_snippet}</p>
                        </button>
                      `,
                    )}
                  </div>
                `}
          `)}
        `)}

        ${Card(html`
          ${CardHeader(html`${CardTitle(selected ? `记忆 #${selected.id}` : "详情")}
          ${CardDescription(selected ? `类别：${formatCleanupCategory(selected.category)}` : "请选择左侧条目")}`)}
          ${CardContent(html`
            ${state.cleanup.detailError
              ? html`<p class="text-sm text-destructive">${state.cleanup.detailError}</p>`
              : state.cleanup.detail
                ? html`
                    <div class="space-y-4">
                      ${state.cleanup.detailLoading ? html`<p class="text-xs text-muted-foreground">正在更新详情...</p>` : ""}
                      ${state.cleanup.detail.migration_target
                        ? html`
                            <section class="rounded-md border border-border/70 bg-background/85 p-3.5">
                              <p class="mb-1 text-xs text-muted-foreground">
                                迁移目标 #${state.cleanup.detail.migration_target.id}
                              </p>
                              <p class="mb-2 text-xs text-muted-foreground">
                                路径：${state.cleanup.detail.migration_target.paths.join(", ") || "（无）"}
                              </p>
                              ${Diff({
                                oldText: state.cleanup.detail.content,
                                newText: state.cleanup.detail.migration_target.content,
                                title: "当前内容与迁移目标差异",
                              })}
                            </section>
                          `
                        : html`
                            <section class="rounded-md border border-border/70 bg-background/85 p-3.5">
                              <p class="mb-2 text-xs text-muted-foreground">当前内容</p>
                              <pre class="max-h-56 overflow-auto whitespace-pre-wrap text-xs leading-5">${state.cleanup.detail
                                .content}</pre>
                            </section>
                          `}
                    </div>
                  `
                : state.cleanup.detailLoading
                  ? html`<p class="text-sm text-muted-foreground">正在加载详情...</p>`
                  : html`<p class="text-sm text-muted-foreground">暂无详情</p>`}
          `)}
          ${CardFooter(html`
            ${Button({
              variant: "destructive",
              disabled: !selected || state.cleanup.deletingId !== null,
              loading: state.cleanup.deletingId === selected?.id,
              children: "永久删除选中项",
              onClick: () => {
                if (!selected) return;
                openConfirmDialog({
                  title: "永久删除",
                  description: `确认永久删除记忆 #${selected.id} 吗？`,
                  confirmText: "确认删除",
                  confirmVariant: "destructive",
                  onConfirm: () => handleDeleteOrphan(selected.id),
                });
              },
            })}
          `)}
        `, true)}
      </div>
    </div>
  `;
}

function renderActivePage(): TemplateResult {
  if (state.activePage === "review") return renderReviewPage();
  if (state.activePage === "memory") return renderMemoryPage();
  return renderCleanupPage();
}

function renderApp() {
  const app = html`
    <div class="app-shell grid-backdrop min-h-screen bg-background text-foreground">
      <div class="flex min-h-screen w-full flex-col gap-4 px-3 py-4 lg:px-5 lg:py-5">
        <header class="app-header panel flex items-center justify-between rounded-xl border border-border px-4 py-3 shadow-sm">
          <div>
            <h1 class="text-base font-semibold tracking-tight lg:text-lg">Nocturne Memory</h1>
          </div>
          <div class="flex items-center gap-2">
            ${Badge(`接口：${formatApiStatusLabel(state.apiOnline)}`, "outline")}
            ${Badge(`页面：${formatPageLabel(state.activePage)}`, "secondary")}
          </div>
        </header>

        <div class="grid flex-1 gap-4 lg:grid-cols-[220px_minmax(0,1fr)]">
          <aside class="space-y-4">
            ${Card(html`
              ${CardHeader(html`${CardTitle("模块导航")}
              ${CardDescription("点击切换模块")}`)}
              ${CardContent(html`
                <div class="space-y-2">
                  ${pageMeta.map(
                    (page) => html`
                      <button
                        type="button"
                        class="w-full cursor-pointer rounded-md border px-3 py-2.5 text-left transition-colors ${page.key ===
                        state.activePage
                          ? "border-primary/20 bg-primary/5 text-foreground"
                          : "border-border/70 bg-background/80 hover:bg-muted/40"}"
                        @click=${(event: Event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          switchPage(page.key);
                        }}
                      >
                        <p class="text-sm font-medium">${page.label}</p>
                        <p class="text-xs text-muted-foreground">${page.subtitle}</p>
                      </button>
                    `,
                  )}
                </div>
              `)}
            `, true)}
          </aside>

          <main class="min-w-0 space-y-4">${renderActivePage()}</main>
        </div>
      </div>

      ${Dialog({
        isOpen: state.confirmDialog.isOpen,
        onClose: () => {
          closeConfirmDialog();
        },
        width: "min(460px, 92vw)",
        children: DialogContent({
          children: html`
            ${DialogHeader({
              title: state.confirmDialog.title,
              description: state.confirmDialog.description,
            })}
            ${DialogFooter({
              children: html`
                ${Button({
                  variant: "outline",
                  disabled: state.confirmDialog.loading,
                  children: "取消",
                  onClick: () => {
                    closeConfirmDialog();
                  },
                })}
                ${Button({
                  variant: state.confirmDialog.confirmVariant,
                  loading: state.confirmDialog.loading,
                  children: state.confirmDialog.confirmText,
                  onClick: () => {
                    void submitConfirmDialog();
                  },
                })}
              `,
            })}
          `,
        }),
      })}
    </div>
  `;

  render(app, rootElement as HTMLElement);
}

state.__subscribe(() => {
  renderApp();
});
reviewState.__subscribe(() => {
  renderApp();
});
memoryState.__subscribe(() => {
  renderApp();
});
cleanupState.__subscribe(() => {
  renderApp();
});
confirmDialogState.__subscribe(() => {
  renderApp();
});

subscribeApiStatus((online) => {
  state.apiOnline = online;
});

renderApp();
void Promise.all([probeApiHealth(), reloadReview(), loadDomainsAndNode(), reloadCleanup()]);

/**
 * Novel Expander - Vue 3 Application
 * AI-powered novel expansion web frontend
 */

const { createApp, ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

const app = createApp({
    setup() {
        const DISMISSED_INTERRUPTED_PREFIX = 'dismissed_interrupted_task_';
        const DISMISSED_FAILED_PREFIX = 'dismissed_failed_task_';
        // ==================== State ====================

        // Novel & chapter data
        const novels = ref([]);
        const currentNovel = ref(null);
        const currentChapter = ref(null);
        const chapters = ref([]);
        const taskHistory = ref([]);
        const dismissedFailedVersion = ref(0);

        // View mode: 'original' | 'expanded' | 'compare'
        const viewMode = ref('original');

        // Expand settings
        const selectedModel = ref('grok-4.20-auto');
        const selectedChapterIds = ref([]);
        const chapterRangeInput = ref('');
        const isExpanding = ref(false);
        const expandTaskId = ref(null);

        // Progress tracking
        const overallProgress = ref(0);
        const completedChapters = ref(0);
        const totalChapters = ref(0);
        const currentExpandingChapter = ref('');
        const progressLogs = ref([]);
        const expandStartTime = ref(null);

        // Paragraph interaction
        const hoveredParagraphIndex = ref(null);
        const editingParagraphIndex = ref(null);
        const editingType = ref(null);
        const instructionText = ref('');
        const streamingParagraphIndex = ref(null);
        const streamingText = ref('');
        const chapterRewritePromptVisible = ref(false);
        const chapterRewriteInstruction = ref('');
        const isChapterRewriting = ref(false);
        const chapterEditorVisible = ref(false);
        const chapterEditorTarget = ref('expanded');
        const chapterEditorText = ref('');
        const chapterEditorSaving = ref(false);

        // UI state
        const showUploadModal = ref(false);
        const showSettingsModal = ref(false);
        const showExportModal = ref(false);
        const showPromptSettingsModal = ref(false);
        const showDeleteConfirm = ref(false);
        const deleteTarget = ref(null);
        const isDragging = ref(false);
        const isUploading = ref(false);
        const leftCollapsed = ref(false);
        const rightCollapsed = ref(false);
        const isMobileLayout = ref(false);
        const mobileNavCollapsed = ref(true);
        const exportFormat = ref('txt');
        const exportSeparatorStyle = ref('classic');
        const queueTasks = ref([]);

        // API Profiles
        const apiProfiles = ref([]);
        const activeProfileId = ref('');
        const activeProfile = ref(null);
        const editingProfileId = ref(null);
        const editingProfileData = reactive({
            name: '',
            api_base: '',
            api_key: '',
            admin_api_key: '',
            default_model: 'grok-4.20-auto',
            model_fallback_order: 'grok-4.20-auto,grok-4.20-fast,grok-4.20-expert',
        });

        // Token status (kept for internal pool checks)
        const tokenStatus = ref(null);

        // SSE connection
        let sseConnection = null;
        let sseNovelId = null;
        let postTaskRefreshTimer = null;
        let expectedSseClose = false;
        let queueRefreshTimer = null;

        // Notifications
        const notifications = ref([]);
        let notifCounter = 0;

        // Settings
        const settings = reactive({
            apiBase: '',
            defaultModel: 'grok-4.20-auto',
            requestDelay: 3,
        });

        // Settings modal state (backend-driven)
        const settingsForm = reactive({});
        const settingsFields = ref({});
        const settingsGroups = ref({});
        const settingsActiveTab = ref('profiles');
        const settingsLoading = ref(false);
        const settingsSaving = ref(false);
        const settingsPasswordVisible = reactive({});

        // Prompt settings modal state
        const promptSettingsLoading = ref(false);
        const promptSettingsSaving = ref(false);
        const promptSettingsActiveGroup = ref('');
        const promptGroups = ref({});
        const promptItems = ref([]);
        const promptForm = reactive({});

        // Available models (SuperGrok)
        const availableModels = [
            { id: 'grok-4.20-auto', name: 'Grok 4.20 Auto', desc: '首选 · 自动额度' },
            { id: 'grok-4.20-fast', name: 'Grok 4.20 Fast', desc: '第二优先 · 速度更快' },
            { id: 'grok-4.20-expert', name: 'Grok 4.20 Expert', desc: '第三优先 · 复杂创作' },
        ];

        // ==================== New State Variables ====================

        // Expand confirm modal
        const showExpandConfirm = ref(false);
        const expandEstimate = ref(null);
        const isRetryingFailed = ref(false);

        // Interrupted task
        const interruptedTask = ref(null);

        // Failed / skipped chapter counts
        const failedChaptersCount = ref(0);
        const skippedChaptersCount = ref(0);

        // Manual paragraph editing
        const manualEditIndex = ref(null);
        const manualEditText = ref('');

        // SSE reconnecting indicator
        const sseReconnecting = ref(false);
        const leftChapterScrollTop = ref(0);
        const rightChapterScrollTop = ref(0);
        const leftChapterListRef = ref(null);
        const rightCheckboxListRef = ref(null);
        const contentBodyRef = ref(null);
        const showChapterTools = ref(false);
        const virtualRowHeight = 40;
        const virtualOverscan = 8;

        // ==================== Computed ====================

        // Paragraphs to display based on current view
        const displayParagraphs = computed(() => {
            if (!currentChapter.value) return [];
            if (viewMode.value === 'expanded' && currentChapter.value.expanded_paragraphs) {
                return currentChapter.value.expanded_paragraphs;
            }
            return currentChapter.value.paragraphs || [];
        });

        // Original paragraphs for compare view
        const originalParagraphs = computed(() => {
            if (!currentChapter.value) return [];
            return currentChapter.value.paragraphs || [];
        });

        // Expanded paragraphs for compare view
        const expandedParagraphs = computed(() => {
            if (!currentChapter.value) return [];
            return currentChapter.value.expanded_paragraphs || [];
        });

        // Has expanded content
        const hasExpanded = computed(() => {
            return !!(currentChapter.value && currentChapter.value.expanded_content);
        });

        // Is currently expanding single chapter
        const isExpandingCurrent = ref(false);

        // Select-all checkbox state
        const allSelected = computed(() => {
            if (chapters.value.length === 0) return false;
            return chapters.value.every(ch => selectedChapterIds.value.includes(ch.id));
        });

        // Estimated remaining time
        const estimatedRemaining = computed(() => {
            if (!isExpanding.value || !expandStartTime.value || overallProgress.value <= 0) {
                return null;
            }
            const elapsed = (Date.now() - expandStartTime.value) / 1000;
            const remaining = (elapsed / overallProgress.value) * (100 - overallProgress.value);
            if (remaining < 60) return `${Math.round(remaining)}s`;
            if (remaining < 3600) return `${Math.round(remaining / 60)}min`;
            return `${Math.round(remaining / 3600)}h`;
        });

        // Can undo expansion (expanded_content_prev exists)
        const canUndo = computed(() => {
            return !!(currentChapter.value && currentChapter.value.expanded_content_prev);
        });

        const currentChapterIndex = computed(() => {
            if (!currentChapter.value) return -1;
            return chapters.value.findIndex(ch => ch.id === currentChapter.value.id);
        });

        const hasPreviousChapter = computed(() => currentChapterIndex.value > 0);
        const hasNextChapter = computed(() => {
            return currentChapterIndex.value >= 0 && currentChapterIndex.value < chapters.value.length - 1;
        });

        // Failed chapter count from chapters list
        const failedChapterCount = computed(() => {
            return chapters.value.filter(ch => ch.status === 'failed' || ch.error_message).length;
        });

        const latestTask = computed(() => taskHistory.value[0] || null);

        const latestRunningTask = computed(() => {
            const tasks = taskHistory.value || [];
            return tasks.find(task => ['queued', 'running', 'pausing', 'paused'].includes(task.status)) || null;
        });

        const currentNovelRunningTask = computed(() => {
            const tasks = taskHistory.value || [];
            return tasks.find(task => task.status === 'running') || null;
        });

        const globalRunningTask = computed(() => {
            const tasks = queueTasks.value || [];
            return tasks.find(task => task.status === 'running' || task.status === 'pausing') || null;
        });

        const liveTaskStatuses = ['running', 'pausing', 'queued', 'paused'];
        const taskStatusRank = {
            running: 0,
            pausing: 0,
            queued: 1,
            paused: 2,
        };

        const sortedQueueTasks = computed(() => {
            return [...(queueTasks.value || [])].sort((a, b) => {
                const rankA = taskStatusRank[a.status] ?? 3;
                const rankB = taskStatusRank[b.status] ?? 3;
                if (rankA !== rankB) return rankA - rankB;
                if (rankA === 1) {
                    const priorityDiff = (b.queue_priority || 0) - (a.queue_priority || 0);
                    if (priorityDiff) return priorityDiff;
                }
                return new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0);
            });
        });

        const canClearTaskHistory = computed(() => {
            return (queueTasks.value || []).some(task => !liveTaskStatuses.includes(task.status));
        });

        const hasOtherNovelRunning = computed(() => {
            return !!(globalRunningTask.value && globalRunningTask.value.novel_id !== currentNovel.value?.id);
        });

        const startExpandLabel = computed(() => {
            return hasOtherNovelRunning.value ? '加入队列' : '开始扩写';
        });

        const latestFailedTask = computed(() => {
            if (!latestTask.value || (latestTask.value.failed_chapters || 0) <= 0) return null;
            return latestTask.value;
        });

        const showFailedTaskAlert = computed(() => {
            if (!latestFailedTask.value || isRetryingFailed.value) return false;
            dismissedFailedVersion.value;
            return localStorage.getItem(`${DISMISSED_FAILED_PREFIX}${latestFailedTask.value.id}`) !== '1';
        });

        // Token warning: active tokens very low
        const tokenWarning = computed(() => {
            if (!tokenStatus.value) return false;
            return tokenStatus.value.active <= 2 && tokenStatus.value.total > 2;
        });

        const progressPercent = computed(() => {
            return Math.min(100, Math.max(0, overallProgress.value));
        });

        const visibleLeftRange = computed(() => {
            const viewport = 420;
            const start = Math.max(0, Math.floor(leftChapterScrollTop.value / virtualRowHeight) - virtualOverscan);
            const end = Math.min(chapters.value.length, start + Math.ceil(viewport / virtualRowHeight) + virtualOverscan * 2);
            return { start, end };
        });

        const visibleRightRange = computed(() => {
            const viewport = 320;
            const start = Math.max(0, Math.floor(rightChapterScrollTop.value / virtualRowHeight) - virtualOverscan);
            const end = Math.min(chapters.value.length, start + Math.ceil(viewport / virtualRowHeight) + virtualOverscan * 2);
            return { start, end };
        });

        const visibleChapters = computed(() => {
            return chapters.value.slice(visibleLeftRange.value.start, visibleLeftRange.value.end);
        });

        const visibleSelectableChapters = computed(() => {
            return chapters.value.slice(visibleRightRange.value.start, visibleRightRange.value.end);
        });

        const leftSpacerTop = computed(() => visibleLeftRange.value.start * virtualRowHeight);
        const leftSpacerBottom = computed(() => Math.max(0, (chapters.value.length - visibleLeftRange.value.end) * virtualRowHeight));
        const rightSpacerTop = computed(() => visibleRightRange.value.start * virtualRowHeight);
        const rightSpacerBottom = computed(() => Math.max(0, (chapters.value.length - visibleRightRange.value.end) * virtualRowHeight));

        // ==================== API Helpers ====================

        function safeApiBase(rawBase) {
            const raw = (rawBase || '').trim().replace(/\/+$/, '');
            if (!raw) return '';

            try {
                const url = new URL(raw, window.location.origin);
                url.username = '';
                url.password = '';
                return url.toString().replace(/\/+$/, '');
            } catch (err) {
                return raw.replace(/\/\/[^/@\s]+@/, '//');
            }
        }

        function appOrigin() {
            const url = new URL(window.location.href);
            url.username = '';
            url.password = '';
            url.pathname = '';
            url.search = '';
            url.hash = '';
            return url.toString().replace(/\/+$/, '');
        }

        function apiUrl(path) {
            return `${appOrigin()}${path}`;
        }

        function apiHeaders(extra = {}) {
            return { ...extra };
        }

        // ==================== Notifications ====================

        function addNotification(msg, type = 'info') {
            const id = ++notifCounter;
            notifications.value.push({ id, msg, type });
            setTimeout(() => {
                removeNotification(id);
            }, 4000);
        }

        function removeNotification(id) {
            const idx = notifications.value.findIndex(n => n.id === id);
            if (idx > -1) notifications.value.splice(idx, 1);
        }

        // ==================== Logging ====================

        function addLog(msg, type = 'info') {
            const now = new Date();
            const time = now.toLocaleTimeString('zh-CN', { hour12: false });
            progressLogs.value.unshift({ time, msg, type });
            if (progressLogs.value.length > 50) {
                progressLogs.value.pop();
            }
        }

        // ==================== Novel Management ====================

        async function loadNovels(retries = 3) {
            for (let attempt = 1; attempt <= retries; attempt++) {
                try {
                    const res = await fetch(apiUrl('/api/novels'), { headers: apiHeaders() });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    novels.value = data.novels || [];
                    return;
                } catch (err) {
                    console.error(`Failed to load novels (attempt ${attempt}/${retries}):`, err);
                    if (attempt < retries) {
                        await new Promise(r => setTimeout(r, 2000));
                    } else {
                        addNotification('加载小说列表失败: ' + err.message, 'error');
                    }
                }
            }
        }

        async function selectNovel(novel) {
            if (currentNovel.value && currentNovel.value.id === novel.id) return;
            disconnectSSE();
            isExpanding.value = false;
            isExpandingCurrent.value = false;
            expandTaskId.value = null;
            currentNovel.value = novel;
            console.log('[selectNovel] Selected novel ID:', novel.id);
            currentChapter.value = null;
            viewMode.value = 'original';
            selectedChapterIds.value = [];
            interruptedTask.value = null;
            try {
                const res = await fetch(apiUrl(`/api/novels/${novel.id}`), { headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                chapters.value = data.chapters || [];
                await loadTaskHistory(novel.id);
                console.log('[selectNovel] Loaded', chapters.value.length, 'chapters');
                // Auto-select all chapters
                selectedChapterIds.value = chapters.value.map(ch => ch.id);
                // Check for interrupted tasks
                checkInterruptedTask();
            } catch (err) {
                console.error('Failed to load novel details:', err);
                addNotification('加载小说详情失败: ' + err.message, 'error');
                chapters.value = [];
                taskHistory.value = [];
            }
        }

        async function loadTaskHistory(novelId = null) {
            const targetNovelId = novelId || currentNovel.value?.id;
            if (!targetNovelId) {
                taskHistory.value = [];
                return;
            }
            try {
                const res = await fetch(apiUrl(`/api/novels/${targetNovelId}/tasks`), { headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                taskHistory.value = data.tasks || [];
                cleanupDismissedTaskFlags();
                restoreRunningTaskFromHistory(targetNovelId);
            } catch (err) {
                console.warn('Failed to load task history:', err);
            }
        }

        async function loadQueueTasks() {
            try {
                const res = await fetch(apiUrl('/api/tasks/queue'), { headers: apiHeaders() });
                if (!res.ok) return;
                const data = await res.json();
                queueTasks.value = data.tasks || [];
                syncCurrentNovelTaskFromQueue();
            } catch (err) {
                console.warn('Failed to load queue tasks:', err);
            }
        }

        async function clearTaskHistory() {
            if (!canClearTaskHistory.value) return;
            if (!confirm('清空已完成、失败、取消和中断的历史任务？正在运行、排队和暂停的任务会保留。')) return;
            try {
                const res = await fetch(apiUrl('/api/tasks/history'), {
                    method: 'DELETE',
                    headers: apiHeaders(),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                await loadQueueTasks();
                if (currentNovel.value) {
                    await loadTaskHistory(currentNovel.value.id);
                }
                addNotification(`已清空 ${data.deleted || 0} 条历史任务`, 'success');
            } catch (err) {
                addNotification('清空任务失败: ' + err.message, 'error');
            }
        }

        function syncCurrentNovelTaskFromQueue() {
            if (!currentNovel.value) return;
            const task = (queueTasks.value || []).find(
                item => item.novel_id === currentNovel.value.id && item.status === 'running'
            );
            if (task) {
                const alreadyTracking = isExpanding.value && expandTaskId.value === task.id && sseConnection;
                expandTaskId.value = task.id;
                isExpanding.value = true;
                isExpandingCurrent.value = false;
                isRetryingFailed.value = false;
                totalChapters.value = task.total_chapters || totalChapters.value || 0;
                completedChapters.value = task.completed_chapters || 0;
                failedChaptersCount.value = task.failed_chapters || 0;
                skippedChaptersCount.value = task.skipped_chapters || 0;
                overallProgress.value = Math.round((task.progress || 0) * 1000) / 10;
                currentExpandingChapter.value = task.current_chapter_title || currentExpandingChapter.value || '';
                if (!expandStartTime.value && task.created_at) {
                    expandStartTime.value = new Date(task.created_at).getTime();
                }
                if (!alreadyTracking) {
                    addLog(`当前小说任务 #${task.id} 已开始运行`, 'info');
                    connectSSE(currentNovel.value.id);
                }
                return;
            }

            const currentTask = expandTaskId.value
                ? (queueTasks.value || []).find(item => item.id === expandTaskId.value)
                : null;
            if (currentTask && !['queued', 'running', 'pausing', 'paused'].includes(currentTask.status)) {
                isExpanding.value = false;
                isExpandingCurrent.value = false;
            }
        }

        async function prioritizeTask(taskId) {
            try {
                const res = await fetch(apiUrl(`/api/tasks/${taskId}/prioritize`), { method: 'POST', headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                await loadQueueTasks();
                addNotification('任务已置顶', 'success');
            } catch (err) {
                addNotification('置顶失败: ' + err.message, 'error');
            }
        }

        async function pauseQueueTask(taskId) {
            try {
                const res = await fetch(apiUrl(`/api/tasks/${taskId}/pause`), { method: 'POST', headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                await loadQueueTasks();
                addNotification('已请求暂停任务', 'warning');
            } catch (err) {
                addNotification('暂停失败: ' + err.message, 'error');
            }
        }

        async function resumeQueueTask(taskId) {
            try {
                const res = await fetch(apiUrl(`/api/tasks/${taskId}/resume`), { method: 'POST', headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                await loadQueueTasks();
                addNotification('任务已恢复并入队', 'success');
            } catch (err) {
                addNotification('恢复失败: ' + err.message, 'error');
            }
        }

        async function cancelTaskById(taskId) {
            try {
                const res = await fetch(apiUrl(`/api/tasks/${taskId}/cancel`), { method: 'POST', headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                await loadQueueTasks();
                addNotification('任务已取消', 'warning');
            } catch (err) {
                addNotification('取消失败: ' + err.message, 'error');
            }
        }

        function restoreRunningTaskFromHistory(novelId) {
            const runningTask = currentNovelRunningTask.value;
            if (!runningTask) {
                if (currentNovel.value?.id === novelId) {
                    isExpanding.value = false;
                    isExpandingCurrent.value = false;
                }
                return;
            }

            const alreadyTracking = isExpanding.value && expandTaskId.value === runningTask.id && sseConnection;
            expandTaskId.value = runningTask.id;
            isExpanding.value = true;
            isExpandingCurrent.value = false;
            isRetryingFailed.value = false;
            totalChapters.value = runningTask.total_chapters || totalChapters.value || 0;
            completedChapters.value = runningTask.completed_chapters || 0;
            failedChaptersCount.value = runningTask.failed_chapters || 0;
            skippedChaptersCount.value = runningTask.skipped_chapters || 0;
            overallProgress.value = Math.round((runningTask.progress || 0) * 1000) / 10;
            currentExpandingChapter.value = runningTask.current_chapter_title || currentExpandingChapter.value || '';
            if (!expandStartTime.value && runningTask.created_at) {
                expandStartTime.value = new Date(runningTask.created_at).getTime();
            }
            if (!alreadyTracking) {
                addLog(`已恢复进行中的扩写任务 #${runningTask.id}`, 'info');
                connectSSE(novelId);
            }
        }

        async function refreshRunningTaskState() {
            if (!currentNovel.value) return false;
            await loadTaskHistory(currentNovel.value.id);
            return !!currentNovelRunningTask.value;
        }

        function cleanupDismissedTaskFlags() {
            const ids = new Set((taskHistory.value || []).map(task => String(task.id)));
            const removeKeys = [];
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (!key) continue;
                if (!key.startsWith(DISMISSED_INTERRUPTED_PREFIX) && !key.startsWith(DISMISSED_FAILED_PREFIX)) {
                    continue;
                }
                const id = key.split('_').pop();
                if (!ids.has(id)) removeKeys.push(key);
            }
            for (const key of removeKeys) localStorage.removeItem(key);
        }

        async function selectChapter(chapter) {
            if (currentChapter.value && currentChapter.value.id === chapter.id) {
                if (isMobileLayout.value || window.innerWidth <= 768) showReadingPanel();
                await nextTick();
                scrollReadingToTop();
                return;
            }

            // Safety: ensure currentNovel is set
            if (!currentNovel.value || !currentNovel.value.id) {
                addNotification('请先选择小说', 'warning');
                return;
            }

            // Reset paragraph interaction state
            editingParagraphIndex.value = null;
            editingType.value = null;
            streamingParagraphIndex.value = null;
            streamingText.value = '';
            hoveredParagraphIndex.value = null;
            manualEditIndex.value = null;
            manualEditText.value = '';
            chapterRewritePromptVisible.value = false;
            chapterRewriteInstruction.value = '';
            showChapterTools.value = false;

            const url = apiUrl(`/api/novels/${currentNovel.value.id}/chapters/${chapter.id}`);
            console.log('[selectChapter] Fetching:', url);

            try {
                const res = await fetch(url, { headers: apiHeaders() });
                console.log('[selectChapter] Response status:', res.status);
                if (!res.ok) {
                    const errBody = await res.text();
                    console.error('[selectChapter] Error body:', errBody);
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                currentChapter.value = data;
                // Switch to expanded view if expanded content is available
                if (data.expanded_content && viewMode.value === 'original') {
                    viewMode.value = 'expanded';
                }
                if (isMobileLayout.value || window.innerWidth <= 768) {
                    showReadingPanel();
                }
                await nextTick();
                scrollReadingToTop();
            } catch (err) {
                console.error('Failed to load chapter:', err);
                addNotification('加载章节内容失败: ' + err.message, 'error');
            }
        }

        function syncMobileLayout() {
            const mobile = window.innerWidth <= 768;
            if (isMobileLayout.value === mobile) return;
            isMobileLayout.value = mobile;
            if (mobile) {
                leftCollapsed.value = true;
                rightCollapsed.value = true;
            } else {
                leftCollapsed.value = false;
                rightCollapsed.value = false;
            }
        }

        function closeMobilePanels() {
            if (!isMobileLayout.value) return;
            leftCollapsed.value = true;
            rightCollapsed.value = true;
        }

        function showReadingPanel() {
            leftCollapsed.value = true;
            rightCollapsed.value = true;
            mobileNavCollapsed.value = true;
        }

        function showCatalogPanel() {
            leftCollapsed.value = false;
            rightCollapsed.value = true;
            mobileNavCollapsed.value = true;
        }

        function openCatalogFromReader() {
            if (isMobileLayout.value) {
                showCatalogPanel();
            } else {
                leftCollapsed.value = false;
            }
        }

        function showTaskPanel() {
            leftCollapsed.value = true;
            rightCollapsed.value = false;
            mobileNavCollapsed.value = true;
        }

        async function deleteNovel(novel) {
            deleteTarget.value = novel;
            showDeleteConfirm.value = true;
        }

        async function confirmDelete() {
            if (!deleteTarget.value) return;
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${deleteTarget.value.id}`),
                    { method: 'DELETE', headers: apiHeaders() }
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                addNotification(`已删除《${deleteTarget.value.title}》`, 'success');
                if (currentNovel.value && currentNovel.value.id === deleteTarget.value.id) {
                    currentNovel.value = null;
                    currentChapter.value = null;
                    chapters.value = [];
                }
                await loadNovels();
            } catch (err) {
                addNotification('删除失败: ' + err.message, 'error');
            }
            showDeleteConfirm.value = false;
            deleteTarget.value = null;
        }

        async function uploadNovel(file) {
            if (!file) return;
            isUploading.value = true;
            try {
                const formData = new FormData();
                formData.append('file', file);
                const res = await fetch(apiUrl('/api/novels/upload'), {
                    method: 'POST',
                    headers: apiHeaders(),
                    body: formData,
                });
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                const data = await res.json();
                addNotification(`上传成功《${data.title}》，共 ${data.chapter_count} 章`, 'success');
                showUploadModal.value = false;
                await loadNovels();
                // Auto-select the uploaded novel
                const uploaded = novels.value.find(n => n.id === data.id);
                if (uploaded) {
                    await selectNovel(uploaded);
                }
            } catch (err) {
                addNotification('上传失败: ' + err.message, 'error');
            } finally {
                isUploading.value = false;
            }
        }

        // ==================== File Upload Handlers ====================

        function onFileInput(event) {
            const file = event.target.files[0];
            if (file) uploadNovel(file);
            event.target.value = '';
        }

        function onDrop(event) {
            event.preventDefault();
            isDragging.value = false;
            const file = event.dataTransfer.files[0];
            if (file) {
                if (!file.name.endsWith('.txt')) {
                    addNotification('请上传 .txt 格式的文件', 'warning');
                    return;
                }
                uploadNovel(file);
            }
        }

        function onDragOver(event) {
            event.preventDefault();
            isDragging.value = true;
        }

        function onDragLeave() {
            isDragging.value = false;
        }

        // ==================== Expand ====================

        // Step 1: Request estimate, show confirm modal
        async function startExpand() {
            if (!currentNovel.value) {
                addNotification('请先选择小说', 'warning');
                return;
            }
            if (selectedChapterIds.value.length === 0) {
                addNotification('请至少选择一个章节', 'warning');
                return;
            }

            // Fetch estimate first
            try {
                const idsParam = encodeURIComponent(JSON.stringify(selectedChapterIds.value));
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/expand/estimate?chapter_ids=${idsParam}`),
                    { headers: apiHeaders() }
                );
                if (res.ok) {
                    expandEstimate.value = await res.json();
                } else {
                    // If estimate fails, still allow proceeding with basic info
                    expandEstimate.value = {
                        chapter_count: selectedChapterIds.value.length,
                        total_chars: 0,
                        estimated_seconds: 0,
                        estimated_minutes: 0,
                        estimated_tokens: 0,
                    };
                }
            } catch (err) {
                expandEstimate.value = {
                    chapter_count: selectedChapterIds.value.length,
                    total_chars: 0,
                    estimated_seconds: 0,
                    estimated_minutes: 0,
                    estimated_tokens: 0,
                };
            }

            showExpandConfirm.value = true;
        }

        // Step 2: User confirmed, actually start expanding
        async function confirmAndStartExpand() {
            showExpandConfirm.value = false;
            expandEstimate.value = null;

            overallProgress.value = 0;
            completedChapters.value = 0;
            failedChaptersCount.value = 0;
            skippedChaptersCount.value = 0;
            totalChapters.value = selectedChapterIds.value.length;
            currentExpandingChapter.value = '';
            progressLogs.value = [];
            expandStartTime.value = Date.now();

            try {
                const body = {
                    chapter_ids: selectedChapterIds.value,
                    model: selectedModel.value,
                };
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/expand`),
                    {
                        method: 'POST',
                        headers: apiHeaders({ 'Content-Type': 'application/json' }),
                        body: JSON.stringify(body),
                    }
                );
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                const data = await res.json();
                expandTaskId.value = data.task_id;
                isRetryingFailed.value = false;
                await loadQueueTasks();
                const isRunningNow = data.status === 'running';
                isExpanding.value = isRunningNow;
                addLog('扩写任务已创建', 'info');
                addNotification(isRunningNow ? '扩写任务已开始' : '任务已加入队列', 'info');

                if (isRunningNow) {
                    connectSSE(currentNovel.value.id);
                }
            } catch (err) {
                const restored = await refreshRunningTaskState();
                isExpanding.value = restored;
                addNotification('启动扩写失败: ' + err.message, 'error');
                addLog('启动失败: ' + err.message, 'error');
            }
        }

        // Expand current chapter (single chapter expand from content header)
        // forceOriginal=true: rewrite from original, false: continue from expanded if available
        async function expandCurrentChapter(forceOriginal = false) {
            if (!currentNovel.value || !currentChapter.value) {
                addNotification('请先选择章节', 'warning');
                return;
            }
            if (latestRunningTask.value) {
                await refreshRunningTaskState();
                if (latestRunningTask.value) {
                    addNotification('当前小说已有扩写任务，请先等待完成或取消当前任务', 'warning');
                    return;
                }
            }

            const chapterId = currentChapter.value.id;
            const useExpandedBase = !forceOriginal && hasExpanded.value;

            isExpandingCurrent.value = false;
            isExpanding.value = false;
            overallProgress.value = 0;
            completedChapters.value = 0;
            failedChaptersCount.value = 0;
            skippedChaptersCount.value = 0;
            totalChapters.value = 1;
            currentExpandingChapter.value = currentChapter.value.title;
            progressLogs.value = [];
            expandStartTime.value = Date.now();

            try {
                const body = {
                    chapter_ids: [chapterId],
                    model: selectedModel.value,
                    use_expanded_as_base: useExpandedBase,
                };
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/expand`),
                    {
                        method: 'POST',
                        headers: apiHeaders({ 'Content-Type': 'application/json' }),
                        body: JSON.stringify(body),
                    }
                );
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                const data = await res.json();
                expandTaskId.value = data.task_id;
                isRetryingFailed.value = false;
                await loadQueueTasks();
                const isRunningNow = data.status === 'running';
                isExpanding.value = isRunningNow;
                isExpandingCurrent.value = isRunningNow;
                addLog(useExpandedBase ? '继续扩写任务已创建（基于已扩写内容）' : '扩写任务已创建', 'info');
                addNotification(
                    isRunningNow
                        ? (useExpandedBase ? '继续扩写已开始' : '扩写已开始')
                        : '任务已加入队列',
                    'info'
                );

                if (isRunningNow) {
                    connectSSE(currentNovel.value.id);
                }
            } catch (err) {
                const restored = await refreshRunningTaskState();
                isExpanding.value = restored;
                isExpandingCurrent.value = false;
                addNotification('启动扩写失败: ' + err.message, 'error');
                addLog('启动失败: ' + err.message, 'error');
            }
        }

        async function cancelExpand() {
            const novelId = currentNovel.value?.id || latestRunningTask.value?.novel_id;
            if (!novelId) {
                addNotification('没有可取消的扩写任务', 'warning');
                return;
            }
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${novelId}/expand/cancel`),
                    {
                        method: 'POST',
                        headers: apiHeaders(),
                    }
                );
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                expectedSseClose = true;
                disconnectSSE();
                isExpanding.value = false;
                isExpandingCurrent.value = false;
                isRetryingFailed.value = false;
                sseReconnecting.value = false;
                currentExpandingChapter.value = '';
                if (currentNovel.value) {
                    await loadTaskHistory(currentNovel.value.id);
                    await refreshNovelDetail(currentNovel.value.id);
                }
                await loadQueueTasks();
                addNotification('已发送取消请求', 'warning');
                addLog('已发送取消请求', 'warning');
            } catch (err) {
                await refreshRunningTaskState();
                addNotification('取消失败: ' + err.message, 'error');
            }
        }

        // ==================== Retry Failed Chapters ====================

        async function retryFailed() {
            if (!currentNovel.value) return;
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/expand/retry-failed`),
                    {
                        method: 'POST',
                        headers: apiHeaders(),
                    }
                );
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                const data = await res.json();
                expandTaskId.value = data.task_id;
                isRetryingFailed.value = true;
                if (latestFailedTask.value?.id) {
                    localStorage.setItem(`${DISMISSED_FAILED_PREFIX}${latestFailedTask.value.id}`, '1');
                }
                overallProgress.value = 0;
                completedChapters.value = 0;
                failedChaptersCount.value = 0;
                skippedChaptersCount.value = 0;
                totalChapters.value = data.retrying_chapters || 0;
                currentExpandingChapter.value = '';
                progressLogs.value = [];
                expandStartTime.value = Date.now();
                addLog(`正在重试 ${data.retrying_chapters} 个失败章节`, 'info');
                await loadQueueTasks();
                const isRunningNow = data.status === 'running';
                isExpanding.value = isRunningNow;
                addNotification(
                    isRunningNow
                        ? `正在重试 ${data.retrying_chapters} 个失败章节`
                        : `已加入队列：重试 ${data.retrying_chapters} 个失败章节`,
                    'info'
                );
                if (isRunningNow) {
                    connectSSE(currentNovel.value.id);
                }
            } catch (err) {
                isRetryingFailed.value = false;
                addNotification('重试失败: ' + err.message, 'error');
            }
        }

        // ==================== Undo Expansion ====================

        async function undoExpansion() {
            if (!currentNovel.value || !currentChapter.value) return;
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/chapters/${currentChapter.value.id}/undo`),
                    { method: 'POST', headers: apiHeaders() }
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                addNotification('已撤销到上一版本', 'success');
                await refreshCurrentChapter();
            } catch (err) {
                addNotification('撤销失败: ' + err.message, 'error');
            }
        }

        // ==================== Interrupted Task ====================

        async function checkInterruptedTask() {
            if (!currentNovel.value) return;
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/expand/interrupted`),
                    { headers: apiHeaders() }
                );
                if (!res.ok) return;
                const data = await res.json();
                if (data.has_interrupted && localStorage.getItem(`${DISMISSED_INTERRUPTED_PREFIX}${data.task_id}`) !== '1') {
                    interruptedTask.value = data;
                } else {
                    interruptedTask.value = null;
                }
            } catch (err) {
                // ignore
            }
        }

        async function resumeTask() {
            if (!currentNovel.value || !interruptedTask.value) return;
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/expand/resume`),
                    {
                        method: 'POST',
                        headers: apiHeaders(),
                    }
                );
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                const data = await res.json();
                expandTaskId.value = data.task_id;
                const isRunningNow = data.status === 'running';
                isExpanding.value = isRunningNow;
                overallProgress.value = 0;
                completedChapters.value = interruptedTask.value.completed_chapters || 0;
                failedChaptersCount.value = interruptedTask.value.failed_chapters || 0;
                skippedChaptersCount.value = interruptedTask.value.skipped_chapters || 0;
                totalChapters.value = interruptedTask.value.total_chapters || 0;
                currentExpandingChapter.value = '';
                progressLogs.value = [];
                expandStartTime.value = Date.now();
                addLog(`从第 ${data.resumed_from_index + 1} 章继续扩写`, 'info');
                addNotification(isRunningNow ? '已恢复中断的扩写任务' : '恢复任务已加入队列', 'info');
                localStorage.removeItem(`${DISMISSED_INTERRUPTED_PREFIX}${data.task_id}`);
                interruptedTask.value = null;
                await loadQueueTasks();
                if (isRunningNow) {
                    connectSSE(currentNovel.value.id);
                }
            } catch (err) {
                addNotification('恢复任务失败: ' + err.message, 'error');
            }
        }

        function dismissInterrupted() {
            if (interruptedTask.value?.task_id) {
                localStorage.setItem(`${DISMISSED_INTERRUPTED_PREFIX}${interruptedTask.value.task_id}`, '1');
            }
            interruptedTask.value = null;
        }

        function dismissFailedAlert() {
            if (latestFailedTask.value?.id) {
                localStorage.setItem(`${DISMISSED_FAILED_PREFIX}${latestFailedTask.value.id}`, '1');
                dismissedFailedVersion.value += 1;
            }
        }

        // ==================== SSE - Expand Progress ====================

        function connectSSE(novelId) {
            if (!novelId) return;
            // Single-connection guard: avoid reconnect storms for the same novel.
            if (sseConnection && sseNovelId === novelId && sseConnection.readyState !== EventSource.CLOSED) {
                return;
            }

            disconnectSSE();
            const url = apiUrl(`/api/novels/${novelId}/expand/stream`);
            const es = new EventSource(url);
            sseConnection = es;
            sseNovelId = novelId;
            sseReconnecting.value = false;
            expectedSseClose = false;

            es.addEventListener('progress', (e) => {
                try {
                    const data = JSON.parse(e.data);
                    overallProgress.value = Math.round((data.overall_progress || 0) * 1000) / 10;
                    completedChapters.value = data.completed_chapters || 0;
                    totalChapters.value = data.total_chapters || totalChapters.value;
                    currentExpandingChapter.value = data.chapter_title || '';

                    // Handle "waiting" status (token pool recovery)
                    if (data.status === 'waiting') {
                        addLog(`\u23F8\uFE0F ${data.message || '等待 Token 池恢复...'}`, 'warning');
                        currentExpandingChapter.value = '\u23F8\uFE0F 等待 Token 恢复...';
                    }

                    // Update failed/skipped counts
                    if (data.failed_chapters !== undefined) {
                        failedChaptersCount.value = data.failed_chapters;
                    }
                    if (data.skipped_chapters !== undefined) {
                        skippedChaptersCount.value = data.skipped_chapters;
                    }

                    // Update chapter status in list
                    const ch = chapters.value.find(c => c.id === data.chapter_id);
                    if (ch) {
                        ch.status = data.status;
                        ch.progress = data.chapter_progress;
                    }

                    if (data.status !== 'waiting') {
                        addLog(`${data.chapter_title}: ${data.status} (${Math.round(data.chapter_progress * 100)}%)`, 'info');
                    }
                } catch (err) {
                    console.error('SSE progress parse error:', err);
                }
            });

            es.addEventListener('chapter_done', async (e) => {
                try {
                    const data = JSON.parse(e.data);
                    const ch = chapters.value.find(c => c.id === data.chapter_id);
                    if (ch) {
                        ch.status = data.status || 'completed';
                        if (data.status === 'completed') {
                            ch.has_expanded = true;
                        }
                    }
                    addLog(
                        `${data.status === 'skipped' ? '章节跳过' : '章节完成'}: ${ch ? ch.title : data.chapter_id}`,
                        data.status === 'skipped' ? 'warning' : 'success'
                    );

                    // Refresh current chapter if it is the one that just finished
                    if (currentChapter.value && currentChapter.value.id === data.chapter_id) {
                        await refreshCurrentChapter();
                    }
                } catch (err) {
                    console.error('SSE chapter_done parse error:', err);
                }
            });

            es.addEventListener('task_done', (e) => {
                let data = {};
                try {
                    data = JSON.parse(e.data);
                } catch (err) {
                    console.warn('SSE task_done parse error:', err);
                }

                isExpanding.value = false;
                isExpandingCurrent.value = false;
                isRetryingFailed.value = false;
                overallProgress.value = data.status === 'failed' ? overallProgress.value : 100;
                sseReconnecting.value = false;
                expectedSseClose = true;
                disconnectSSE();

                if (data.completed_chapters !== undefined) {
                    completedChapters.value = data.completed_chapters;
                }
                if (data.failed_chapters !== undefined) {
                    failedChaptersCount.value = data.failed_chapters;
                }
                if (data.skipped_chapters !== undefined) {
                    skippedChaptersCount.value = data.skipped_chapters;
                }

                if (data.status === 'completed') {
                    addLog('扩写任务完成', 'success');
                    addNotification('扩写任务已完成', 'success');
                } else if (data.status === 'cancelled') {
                    addLog('扩写任务已取消', 'warning');
                    addNotification('扩写任务已取消', 'warning');
                } else {
                    addLog(`扩写任务结束：${data.status || 'unknown'}`, 'warning');
                    addNotification(
                        data.error ? `扩写任务失败: ${data.error}` : '扩写任务未完整完成',
                        'error'
                    );
                }

                // Refresh data with debounce to avoid burst polling.
                if (postTaskRefreshTimer) {
                    clearTimeout(postTaskRefreshTimer);
                    postTaskRefreshTimer = null;
                }
                postTaskRefreshTimer = setTimeout(() => {
                    loadNovels();
                    if (currentNovel.value) {
                        refreshNovelDetail(currentNovel.value.id);
                        checkInterruptedTask();
                        loadTaskHistory(currentNovel.value.id);
                    }
                    loadQueueTasks();
                    postTaskRefreshTimer = null;
                }, 300);
                // Refresh current chapter to show expanded content
                if (currentChapter.value) {
                    refreshCurrentChapter().then(() => {
                        if (hasExpanded.value) viewMode.value = 'expanded';
                    });
                }
            });

            es.addEventListener('error', (e) => {
                // SSE error event can be from EventSource reconnect or actual error
                try {
                    if (e.data) {
                        const data = JSON.parse(e.data);
                        addLog(`错误: ${data.error || '未知错误'}`, 'error');
                        addNotification(`扩写错误: ${data.error}`, 'error');
                        const ch = chapters.value.find(c => c.id === data.chapter_id);
                        if (ch) ch.status = 'failed';
                    }
                } catch (err) {
                    // EventSource connection error
                }
            });

            es.addEventListener('heartbeat', () => {
                // Keep-alive, no action needed
            });

            es.onopen = () => {
                sseReconnecting.value = false;
            };

            es.onerror = (e) => {
                // If the connection is closed naturally after task_done, don't warn
                if (!isExpanding.value || expectedSseClose) {
                    disconnectSSE();
                    return;
                }
                sseReconnecting.value = true;
                addLog('SSE 连接断开，自动重连中...', 'warning');
                console.warn('SSE connection error, will auto-reconnect');
            };
        }

        function disconnectSSE() {
            if (sseConnection) {
                sseConnection.close();
                sseConnection = null;
            }
            sseNovelId = null;
            sseReconnecting.value = false;
        }

        async function refreshCurrentChapter() {
            if (!currentNovel.value || !currentChapter.value) return;
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/chapters/${currentChapter.value.id}`),
                    { headers: apiHeaders() }
                );
                if (!res.ok) return;
                const data = await res.json();
                currentChapter.value = data;
                await nextTick();
                scrollReadingToTop();
            } catch (err) {
                console.error('Failed to refresh chapter:', err);
            }
        }

        async function refreshNovelDetail(novelId) {
            try {
                const res = await fetch(apiUrl(`/api/novels/${novelId}`), { headers: apiHeaders() });
                if (!res.ok) return;
                const data = await res.json();
                chapters.value = data.chapters || [];
            } catch (err) {
                console.error('Failed to refresh novel detail:', err);
            }
        }

        function escapeHtml(text) {
            return (text || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function renderCompareDiff(index, side) {
            const orig = (currentChapter.value?.paragraphs || []).find(p => p.index === index)?.text || '';
            const exp = (currentChapter.value?.expanded_paragraphs || []).find(p => p.index === index)?.text || '';
            const base = side === 'original' ? orig : exp;
            const other = side === 'original' ? exp : orig;
            if (!base || !other || base === other) return escapeHtml(base);

            let prefix = 0;
            while (prefix < base.length && prefix < other.length && base[prefix] === other[prefix]) {
                prefix++;
            }

            let suffix = 0;
            while (
                suffix < base.length - prefix &&
                suffix < other.length - prefix &&
                base[base.length - 1 - suffix] === other[other.length - 1 - suffix]
            ) {
                suffix++;
            }

            const start = escapeHtml(base.slice(0, prefix));
            const middle = escapeHtml(base.slice(prefix, base.length - suffix));
            const end = escapeHtml(base.slice(base.length - suffix));
            return middle ? `${start}<span class="diff-highlight">${middle}</span>${end}` : escapeHtml(base);
        }

        // ==================== Paragraph Operations ====================

        function getParaDisplayText(para) {
            // If this paragraph is currently being streamed, show streaming text
            if (streamingParagraphIndex.value === para.index) {
                return streamingText.value;
            }
            return para.text;
        }

        function isStreamingPara(index) {
            return streamingParagraphIndex.value === index;
        }

        function showChapterRewriteInstruction() {
            if (!currentChapter.value) {
                addNotification('请先选择章节', 'warning');
                return;
            }
            if (isExpanding.value || latestRunningTask.value) {
                addNotification('已有扩写任务正在运行，请先等待完成或取消当前任务', 'warning');
                return;
            }
            chapterRewritePromptVisible.value = true;
            chapterRewriteInstruction.value = '';
            nextTick(() => {
                const el = document.querySelector('.instruction-input-wrap textarea');
                if (el) el.focus();
            });
        }

        function cancelChapterRewriteInstruction() {
            chapterRewritePromptVisible.value = false;
            chapterRewriteInstruction.value = '';
        }

        function onChapterRewriteKeydown(event) {
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.preventDefault();
                submitChapterRewriteInstruction();
            } else if (event.key === 'Escape') {
                event.preventDefault();
                cancelChapterRewriteInstruction();
            }
        }

        async function submitChapterRewriteInstruction() {
            if (!currentNovel.value || !currentChapter.value) return;

            const instruction = chapterRewriteInstruction.value.trim();
            if (!instruction) {
                addNotification('请输入整章重写指令', 'warning');
                return;
            }
            if (isChapterRewriting.value) return;

            isChapterRewriting.value = true;
            isExpandingCurrent.value = true;
            chapterRewritePromptVisible.value = false;
            addNotification('整章指令重写已开始', 'info');
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/chapters/${currentChapter.value.id}/rewrite`),
                    {
                        method: 'POST',
                        headers: apiHeaders({ 'Content-Type': 'application/json' }),
                        body: JSON.stringify({
                            instruction,
                            model: selectedModel.value,
                        }),
                    }
                );

                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }

                await readSSEStream(res, {
                    onDone() {},
                    onError(err) {
                        throw err;
                    },
                });

                await refreshCurrentChapter();
                viewMode.value = 'expanded';
                chapterRewriteInstruction.value = '';
                addNotification('整章指令重写完成', 'success');
            } catch (err) {
                chapterRewritePromptVisible.value = true;
                addNotification('整章指令重写失败: ' + err.message, 'error');
            } finally {
                isChapterRewriting.value = false;
                isExpandingCurrent.value = false;
            }
        }

        function cancelEditing() {
            editingParagraphIndex.value = null;
            editingType.value = null;
            instructionText.value = '';
        }

        // Generic POST SSE stream reader using fetch
        async function readSSEStream(response, handlers) {
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                // SSE messages are separated by double newlines
                const messages = buffer.split('\n\n');
                buffer = messages.pop(); // Keep incomplete message in buffer

                for (const message of messages) {
                    if (!message.trim()) continue;

                    let eventType = '';
                    let dataStr = '';

                    for (const line of message.split('\n')) {
                        const trimmed = line.trim();
                        if (trimmed.startsWith('event:')) {
                            eventType = trimmed.slice(6).trim();
                        } else if (trimmed.startsWith('data:')) {
                            dataStr = trimmed.slice(5).trim();
                        }
                    }

                    if (!dataStr) continue;

                    try {
                        const data = JSON.parse(dataStr);
                        if (eventType === 'done' || data.new_text !== undefined || data.full_text !== undefined) {
                            if (handlers.onDone) handlers.onDone(data);
                        } else if (eventType === 'chunk' || data.text !== undefined) {
                            if (handlers.onChunk) handlers.onChunk(data);
                        }
                    } catch (e) {
                        console.warn('SSE parse error:', e, dataStr);
                    }
                }
            }

            // Process any remaining buffer
            if (buffer.trim()) {
                let dataStr = '';
                let eventType = '';
                for (const line of buffer.split('\n')) {
                    const trimmed = line.trim();
                    if (trimmed.startsWith('event:')) {
                        eventType = trimmed.slice(6).trim();
                    } else if (trimmed.startsWith('data:')) {
                        dataStr = trimmed.slice(5).trim();
                    }
                }
                if (dataStr) {
                    try {
                        const data = JSON.parse(dataStr);
                        if (eventType === 'done' || data.new_text !== undefined || data.full_text !== undefined) {
                            if (handlers.onDone) handlers.onDone(data);
                        } else if (data.text !== undefined) {
                            if (handlers.onChunk) handlers.onChunk(data);
                        }
                    } catch (e) {
                        // Ignore
                    }
                }
            }
        }

        // Update paragraph text in current chapter data
        function updateParagraphText(paragraphIndex, newText) {
            if (!currentChapter.value) return;

            // Determine which paragraphs array to update based on viewMode
            const useExpanded = viewMode.value === 'expanded' && currentChapter.value.expanded_paragraphs;
            const paragraphs = useExpanded
                ? currentChapter.value.expanded_paragraphs
                : currentChapter.value.paragraphs;

            if (paragraphs) {
                const para = paragraphs.find(p => p.index === paragraphIndex);
                if (para) {
                    para.text = newText;
                }
            }

            // Also refresh from server after a short delay
            setTimeout(() => refreshCurrentChapter(), 1000);
        }

        function isParagraphDifferent(index) {
            if (!currentChapter.value || !currentChapter.value.expanded_paragraphs) return false;
            const orig = (currentChapter.value.paragraphs || []).find(p => p.index === index)?.text || '';
            const exp = (currentChapter.value.expanded_paragraphs || []).find(p => p.index === index)?.text || '';
            if (!orig && !exp) return false;
            return orig !== exp;
        }

        // ==================== Manual Paragraph Editing ====================

        function startEditParagraph(index) {
            // Don't start editing if streaming or already editing with instruction
            if (streamingParagraphIndex.value === index) return;
            if (editingParagraphIndex.value === index && editingType.value !== null) return;

            const useExpanded = viewMode.value === 'expanded' && currentChapter.value && currentChapter.value.expanded_paragraphs;
            const paragraphs = useExpanded
                ? currentChapter.value.expanded_paragraphs
                : (currentChapter.value ? currentChapter.value.paragraphs : []);

            const para = paragraphs ? paragraphs.find(p => p.index === index) : null;
            if (!para) return;

            manualEditIndex.value = index;
            manualEditText.value = para.text;

            nextTick(() => {
                const el = document.querySelector('.para-edit-textarea');
                if (el) el.focus();
            });
        }

        function cancelManualEdit() {
            manualEditIndex.value = null;
            manualEditText.value = '';
        }

        function openChapterEditor(target) {
            if (!currentChapter.value) return;
            chapterEditorTarget.value = target === 'original' ? 'original' : 'expanded';
            if (chapterEditorTarget.value === 'original') {
                chapterEditorText.value = currentChapter.value.original_content || '';
            } else {
                chapterEditorText.value = currentChapter.value.expanded_content || currentChapter.value.original_content || '';
            }
            chapterEditorVisible.value = true;
            nextTick(() => {
                const el = document.querySelector('.chapter-edit-textarea');
                if (el) el.focus();
            });
        }

        function closeChapterEditor() {
            if (chapterEditorSaving.value) return;
            chapterEditorVisible.value = false;
            chapterEditorText.value = '';
        }

        async function saveChapterEditor() {
            if (!currentNovel.value || !currentChapter.value) return;
            const content = chapterEditorText.value || '';
            if (!content.trim()) {
                addNotification('章节内容不能为空', 'warning');
                return;
            }
            chapterEditorSaving.value = true;
            const isExpanded = chapterEditorTarget.value !== 'original';
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/chapters/${currentChapter.value.id}/save-content`),
                    {
                        method: 'POST',
                        headers: apiHeaders({ 'Content-Type': 'application/json' }),
                        body: JSON.stringify({ content, is_expanded: isExpanded }),
                    }
                );
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || errData.error || `HTTP ${res.status}`);
                }
                addNotification(isExpanded ? '扩写内容已保存' : '原文已保存', 'success');
                chapterEditorVisible.value = false;
                await refreshCurrentChapter();
                await refreshNovelDetail(currentNovel.value.id);
                viewMode.value = isExpanded ? 'expanded' : 'original';
            } catch (err) {
                addNotification('保存失败: ' + err.message, 'error');
            } finally {
                chapterEditorSaving.value = false;
            }
        }

        async function saveManualEdit() {
            if (manualEditIndex.value === null || !currentNovel.value || !currentChapter.value) return;
            const idx = manualEditIndex.value;
            const newText = manualEditText.value;
            if (!newText.trim()) {
                addNotification('段落内容不能为空', 'warning');
                return;
            }

            // Determine which paragraphs to use
            const useExpanded = viewMode.value === 'expanded' && currentChapter.value.expanded_paragraphs;
            const paragraphs = useExpanded
                ? currentChapter.value.expanded_paragraphs
                : currentChapter.value.paragraphs;

            if (!paragraphs) return;

            // Build full content
            const allTexts = paragraphs.map(p => p.index === idx ? newText : p.text);
            const fullContent = allTexts.join('\n\n');

            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/chapters/${currentChapter.value.id}/save-content`),
                    {
                        method: 'POST',
                        headers: apiHeaders({ 'Content-Type': 'application/json' }),
                        body: JSON.stringify({ content: fullContent, is_expanded: !!useExpanded }),
                    }
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);

                // Update locally
                const para = paragraphs.find(p => p.index === idx);
                if (para) para.text = newText;

                addNotification('段落已保存', 'success');
                setTimeout(() => refreshCurrentChapter(), 500);
            } catch (err) {
                addNotification('保存失败: ' + err.message, 'error');
            }

            manualEditIndex.value = null;
            manualEditText.value = '';
        }

        // ==================== Chapter Selection ====================

        function toggleAllChapters() {
            if (allSelected.value) {
                selectedChapterIds.value = [];
            } else {
                selectedChapterIds.value = chapters.value.map(ch => ch.id);
            }
        }

        function toggleChapter(chapterId) {
            const idx = selectedChapterIds.value.indexOf(chapterId);
            if (idx > -1) {
                selectedChapterIds.value.splice(idx, 1);
            } else {
                selectedChapterIds.value.push(chapterId);
            }
        }

        function isChapterSelected(chapterId) {
            return selectedChapterIds.value.includes(chapterId);
        }

        function applyChapterRange() {
            const raw = chapterRangeInput.value.trim();
            if (!raw) return;
            // Support formats: "206-300", "5-10", "3", "1,3,5-8"
            const ids = new Set();
            const parts = raw.split(/[,，]/);
            for (const part of parts) {
                const trimmed = part.trim();
                const rangeMatch = trimmed.match(/^(\d+)\s*[-~～到]\s*(\d+)$/);
                if (rangeMatch) {
                    const start = parseInt(rangeMatch[1], 10);
                    const end = parseInt(rangeMatch[2], 10);
                    for (let i = Math.min(start, end); i <= Math.max(start, end); i++) {
                        if (i >= 1 && i <= chapters.value.length) {
                            ids.add(chapters.value[i - 1].id);
                        }
                    }
                } else if (/^\d+$/.test(trimmed)) {
                    const num = parseInt(trimmed, 10);
                    if (num >= 1 && num <= chapters.value.length) {
                        ids.add(chapters.value[num - 1].id);
                    }
                }
            }
            if (ids.size === 0) {
                addNotification('无效的范围格式，请输入如 206-300', 'warning');
                return;
            }
            selectedChapterIds.value = Array.from(ids);
            addNotification(`已选中 ${ids.size} 章`, 'success');
        }

        // ==================== Export ====================

        async function exportNovel() {
            if (!currentNovel.value) {
                addNotification('请先选择小说', 'warning');
                return;
            }
            try {
                const res = await fetch(
                    apiUrl(`/api/novels/${currentNovel.value.id}/export?format=${encodeURIComponent(exportFormat.value)}&separator_style=${encodeURIComponent(exportSeparatorStyle.value)}`),
                    { headers: apiHeaders() }
                );
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${currentNovel.value.title || 'novel'}.${exportFormat.value}`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                addNotification('导出成功', 'success');
            } catch (err) {
                addNotification('导出失败: ' + err.message, 'error');
            }
        }

        // ==================== Settings ====================

        async function loadSettings() {
            // Load local UI-only settings (apiBase for frontend routing)
            try {
                const saved = localStorage.getItem('novel-expander-settings');
                if (saved) {
                    const parsed = JSON.parse(saved);
                    Object.assign(settings, parsed);
                    settings.apiBase = safeApiBase(settings.apiBase);
                    if (parsed.apiBase !== settings.apiBase) {
                        localStorage.setItem('novel-expander-settings', JSON.stringify({ ...settings }));
                    }
                    selectedModel.value = settings.defaultModel || 'grok-4.20-auto';
                    if (parsed.selectedModel) {
                        selectedModel.value = parsed.selectedModel;
                    }
                }
            } catch (e) {
                // Ignore
            }
        }

        function saveExpansionSettings(closeModal = false) {
            settings.selectedModel = selectedModel.value;
            settings.defaultModel = selectedModel.value || settings.defaultModel || 'grok-4.20-auto';
            localStorage.setItem('novel-expander-settings', JSON.stringify({ ...settings }));
            if (closeModal) {
                showSettingsModal.value = false;
                addNotification('扩写配置已保存', 'success');
            }
        }

        async function openSettingsModal() {
            showSettingsModal.value = true;
            settingsActiveTab.value = 'profiles';
            settingsLoading.value = true;
            try {
                // Load profiles and settings in parallel
                const [profilesRes, settingsRes] = await Promise.all([
                    fetch(apiUrl('/api/profiles'), { headers: apiHeaders() }),
                    fetch(apiUrl('/api/settings'), { headers: apiHeaders() }),
                ]);

                // Process profiles
                if (profilesRes.ok) {
                    const pData = await profilesRes.json();
                    apiProfiles.value = pData.profiles || [];
                    activeProfileId.value = pData.active_profile_id || '';
                    activeProfile.value = apiProfiles.value.find(p => p.id === activeProfileId.value) || null;
                }

                // Process settings
                if (!settingsRes.ok) throw new Error(`HTTP ${settingsRes.status}`);
                const data = await settingsRes.json();

                // Populate form with current values
                const currentSettings = data.settings || {};
                Object.keys(settingsForm).forEach(k => delete settingsForm[k]);
                Object.assign(settingsForm, currentSettings);

                // Populate metadata
                settingsFields.value = data.meta?.fields || {};
                settingsGroups.value = data.meta?.groups || {};

                // Init password visibility state
                Object.keys(settingsFields.value).forEach(key => {
                    if (settingsFields.value[key].type === 'password' && !(key in settingsPasswordVisible)) {
                        settingsPasswordVisible[key] = false;
                    }
                });
            } catch (err) {
                addNotification('加载设置失败: ' + err.message, 'error');
            } finally {
                settingsLoading.value = false;
            }
        }

        async function saveSettings() {
            settingsSaving.value = true;
            try {
                const res = await fetch(apiUrl('/api/settings'), {
                    method: 'PUT',
                    headers: apiHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ settings: { ...settingsForm } }),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();

                // Update local UI settings
                if (data.settings) {
                    if (data.settings.api_base !== undefined) settings.apiBase = data.settings.api_base;
                    if (data.settings.default_model !== undefined) {
                        settings.defaultModel = data.settings.default_model;
                        selectedModel.value = data.settings.default_model;
                    }
                    if (data.settings.request_delay !== undefined) settings.requestDelay = data.settings.request_delay;
                }

                // Persist local settings for page reload
                saveExpansionSettings(false);

                addNotification('设置已保存', 'success');
                showSettingsModal.value = false;
            } catch (err) {
                addNotification('保存设置失败: ' + err.message, 'error');
            } finally {
                settingsSaving.value = false;
            }
        }

        async function resetSettings() {
            if (!confirm('确定要恢复所有设置为默认值吗？')) return;
            settingsSaving.value = true;
            try {
                const res = await fetch(apiUrl('/api/settings/reset'), {
                    method: 'POST',
                    headers: apiHeaders(),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();

                // Reload form with defaults
                Object.keys(settingsForm).forEach(k => delete settingsForm[k]);
                Object.assign(settingsForm, data.settings || {});

                // Update local settings
                if (data.settings) {
                    settings.apiBase = data.settings.api_base || '';
                    settings.defaultModel = data.settings.default_model || 'grok-4.20-auto';
                    settings.requestDelay = data.settings.request_delay || 3;
                    selectedModel.value = settings.defaultModel;
                }
                saveExpansionSettings(false);

                addNotification('已恢复默认设置', 'success');
            } catch (err) {
                addNotification('恢复默认失败: ' + err.message, 'error');
            } finally {
                settingsSaving.value = false;
            }
        }

        // ==================== Prompt Settings ====================

        function applyPromptSettingsResponse(data) {
            promptGroups.value = data.groups || {};
            promptItems.value = data.prompts || [];
            Object.keys(promptForm).forEach(k => delete promptForm[k]);
            promptItems.value.forEach(item => {
                promptForm[item.key] = item.value || '';
            });
            const groupKeys = Object.keys(promptGroups.value);
            if (!promptSettingsActiveGroup.value || !promptGroups.value[promptSettingsActiveGroup.value]) {
                promptSettingsActiveGroup.value = groupKeys[0] || '';
            }
        }

        async function openPromptSettingsModal() {
            showPromptSettingsModal.value = true;
            promptSettingsLoading.value = true;
            try {
                const res = await fetch(apiUrl('/api/prompts'), { headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                applyPromptSettingsResponse(await res.json());
            } catch (err) {
                addNotification('加载提示词失败: ' + err.message, 'error');
            } finally {
                promptSettingsLoading.value = false;
            }
        }

        async function savePromptSettings() {
            promptSettingsSaving.value = true;
            try {
                const res = await fetch(apiUrl('/api/prompts'), {
                    method: 'PUT',
                    headers: apiHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ prompts: { ...promptForm } }),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                applyPromptSettingsResponse(await res.json());
                addNotification('提示词已保存', 'success');
                showPromptSettingsModal.value = false;
            } catch (err) {
                addNotification('保存提示词失败: ' + err.message, 'error');
            } finally {
                promptSettingsSaving.value = false;
            }
        }

        async function resetPromptSettings() {
            if (!confirm('确定要恢复所有提示词为默认值吗？')) return;
            promptSettingsSaving.value = true;
            try {
                const res = await fetch(apiUrl('/api/prompts/reset'), {
                    method: 'POST',
                    headers: apiHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({}),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                applyPromptSettingsResponse(await res.json());
                addNotification('提示词已恢复默认', 'success');
            } catch (err) {
                addNotification('恢复提示词失败: ' + err.message, 'error');
            } finally {
                promptSettingsSaving.value = false;
            }
        }

        function resetSinglePrompt(key) {
            const item = promptItems.value.find(p => p.key === key);
            if (!item) return;
            promptForm[key] = item.default || '';
        }

        // ==================== Utility ====================

        function chapterStatusIcon(status) {
            const map = {
                'pending': '\u2B1C',
                'analyzing': '\uD83D\uDD0D',
                'expanding': '\u270F\uFE0F',
                'completed': '\u2705',
                'failed': '\u274C',
                'skipped': '\u23ED\uFE0F',
                'waiting': '\u23F8\uFE0F',
            };
            return map[status] || '\u2B1C';
        }

        function chapterStatusText(chapter) {
            if (!chapter) return '';
            if (chapter.error_message) return `失败：${chapter.error_message}`;
            if (chapter.status === 'skipped') return '已跳过，无需扩写';
            if (chapter.status === 'completed') return '已完成';
            if (chapter.status === 'expanding') return '扩写中';
            if (chapter.status === 'failed') return '扩写失败';
            return '待处理';
        }

        function taskStatusText(task) {
            const map = {
                completed: '已完成',
                failed: '失败',
                cancelled: '已取消',
                interrupted: '已中断',
                running: '运行中',
                pausing: '暂停中',
                queued: '排队中',
                paused: '已暂停',
            };
            return map[task?.status] || (task?.status || '');
        }

        function taskStatusClass(task) {
            return task?.status ? `task-${task.status}` : '';
        }

        function taskProgressPercent(task) {
            const raw = Number(task?.progress || 0) * 100;
            return Math.max(0, Math.min(100, Math.round(raw * 10) / 10));
        }

        function formatDate(dateStr) {
            if (!dateStr) return '';
            const d = new Date(dateStr);
            return d.toLocaleDateString('zh-CN', {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
            });
        }

        function formatWordCount(count) {
            if (!count) return '0';
            if (count >= 10000) return (count / 10000).toFixed(1) + '\u4E07';
            if (count >= 1000) return (count / 1000).toFixed(1) + 'k';
            return String(count);
        }

        function formatDuration(seconds) {
            if (seconds === null || seconds === undefined) return '-';
            if (seconds < 60) return `${seconds}s`;
            if (seconds < 3600) return `${Math.round(seconds / 60)}min`;
            return `${(seconds / 3600).toFixed(1)}h`;
        }

        function onLeftChapterScroll(event) {
            leftChapterScrollTop.value = event.target.scrollTop;
        }

        function onRightChapterScroll(event) {
            rightChapterScrollTop.value = event.target.scrollTop;
        }

        function scrollReadingToTop() {
            const el = contentBodyRef.value || document.querySelector('.content-body');
            if (el && typeof el.scrollTo === 'function') {
                el.scrollTo({ top: 0, left: 0, behavior: 'auto' });
            } else if (el) {
                el.scrollTop = 0;
            }
            const compareColumns = document.querySelectorAll('.compare-column .reading-area');
            compareColumns.forEach(column => {
                if ('scrollTop' in column) column.scrollTop = 0;
            });
        }

        function selectRelativeChapter(offset) {
            if (!chapters.value.length) return;
            const currentIndex = currentChapter.value
                ? chapters.value.findIndex(ch => ch.id === currentChapter.value.id)
                : -1;
            const nextIndex = Math.min(chapters.value.length - 1, Math.max(0, currentIndex + offset));
            const target = chapters.value[nextIndex];
            if (target) selectChapter(target);
        }

        function handleGlobalKeydown(event) {
            const tag = event.target?.tagName?.toLowerCase();
            if (tag === 'input' || tag === 'textarea' || event.target?.isContentEditable) return;

            if (event.key === 'j' || event.key === 'J') {
                event.preventDefault();
                selectRelativeChapter(1);
            } else if (event.key === 'k' || event.key === 'K') {
                event.preventDefault();
                selectRelativeChapter(-1);
            } else if (event.key === 'e' || event.key === 'E') {
                event.preventDefault();
                if (currentChapter.value) {
                    if (hasExpanded.value) {
                        viewMode.value = viewMode.value === 'expanded' ? 'original' : 'expanded';
                    } else {
                        expandCurrentChapter(false);
                    }
                }
            } else if ((event.key === 'u' || event.key === 'U') && canUndo.value) {
                event.preventDefault();
                undoExpansion();
            }
        }

        // ==================== API Profiles ====================

        async function loadProfiles() {
            try {
                const res = await fetch(apiUrl('/api/profiles'), { headers: apiHeaders() });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                apiProfiles.value = data.profiles || [];
                activeProfileId.value = data.active_profile_id || '';
                activeProfile.value = apiProfiles.value.find(p => p.id === activeProfileId.value) || null;
                // Sync selectedModel from active profile
                if (activeProfile.value) {
                    selectedModel.value = activeProfile.value.default_model || 'grok-4.20-auto';
                }
            } catch (err) {
                console.warn('Failed to load profiles:', err);
            }
        }

        function addNewProfile() {
            const newProfile = {
                id: `new-${Date.now()}`,
                name: `配置${apiProfiles.value.length + 1}`,
                api_base: '',
                api_key: '',
                admin_api_key: '',
                default_model: 'grok-4.20-auto',
                model_fallback_order: 'grok-4.20-auto,grok-4.20-fast,grok-4.20-expert',
                has_api_key: false,
                has_admin_api_key: false,
                is_new: true,
            };
            apiProfiles.value.push(newProfile);
            editProfile(newProfile);
        }

        function editProfile(profile) {
            editingProfileId.value = profile.id;
            editingProfileData.name = profile.name;
            editingProfileData.api_base = profile.api_base;
            editingProfileData.api_key = '';
            editingProfileData.admin_api_key = '';
            editingProfileData.default_model = profile.default_model || 'grok-4.20-auto';
            editingProfileData.model_fallback_order = profile.model_fallback_order || 'grok-4.20-auto,grok-4.20-fast,grok-4.20-expert';
        }

        async function saveProfile() {
            if (!editingProfileId.value) return;
            try {
                const isNew = String(editingProfileId.value).startsWith('new-');
                const res = await fetch(apiUrl(isNew ? '/api/profiles' : `/api/profiles/${editingProfileId.value}`), {
                    method: isNew ? 'POST' : 'PUT',
                    headers: apiHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ ...editingProfileData }),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                // Update local list
                const idx = apiProfiles.value.findIndex(p => p.id === editingProfileId.value);
                if (idx !== -1) {
                    apiProfiles.value[idx] = data.profile;
                }
                // If edited the active profile, update activeProfile ref
                if (editingProfileId.value === activeProfileId.value) {
                    activeProfile.value = data.profile;
                    selectedModel.value = data.profile.default_model || 'grok-4.20-auto';
                }
                editingProfileId.value = null;
                addNotification('配置已保存', 'success');
            } catch (err) {
                addNotification('保存配置失败: ' + err.message, 'error');
            }
        }

        function cancelEditProfile() {
            editingProfileId.value = null;
        }

        async function deleteProfile(profileId) {
            if (!confirm('确定要删除此配置吗？')) return;
            try {
                const res = await fetch(apiUrl(`/api/profiles/${profileId}`), {
                    method: 'DELETE',
                    headers: apiHeaders(),
                });
                if (!res.ok) {
                    const errData = await res.json().catch(() => ({}));
                    throw new Error(errData.detail || `HTTP ${res.status}`);
                }
                const data = await res.json();
                apiProfiles.value = apiProfiles.value.filter(p => p.id !== profileId);
                activeProfileId.value = data.active_profile_id;
                activeProfile.value = apiProfiles.value.find(p => p.id === activeProfileId.value) || null;
                if (editingProfileId.value === profileId) {
                    editingProfileId.value = null;
                }
                addNotification('配置已删除', 'success');
            } catch (err) {
                addNotification('删除配置失败: ' + err.message, 'error');
            }
        }

        async function switchProfile(profileId) {
            try {
                const res = await fetch(apiUrl(`/api/profiles/${profileId}/switch`), {
                    method: 'POST',
                    headers: apiHeaders(),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                activeProfileId.value = profileId;
                activeProfile.value = data.profile;
                // Update local list to reflect any changes
                const idx = apiProfiles.value.findIndex(p => p.id === profileId);
                if (idx !== -1) {
                    apiProfiles.value[idx] = data.profile;
                }
                selectedModel.value = data.profile.default_model || 'grok-4.20-auto';
                addNotification(`已切换到 ${data.profile.name}`, 'success');
            } catch (err) {
                addNotification('切换配置失败: ' + err.message, 'error');
            }
        }

        // ==================== Token Status ====================

        async function fetchTokenStatus() {
            try {
                const res = await fetch(apiUrl('/api/token-status'), { headers: apiHeaders() });
                if (!res.ok) return;
                tokenStatus.value = await res.json();
            } catch (err) {
                console.warn('Failed to fetch token status:', err);
            }
        }

        // ==================== Lifecycle ====================

        onMounted(() => {
            syncMobileLayout();
            loadSettings();
            loadNovels();
            loadProfiles();
            loadQueueTasks();
            queueRefreshTimer = setInterval(loadQueueTasks, 5000);
            window.addEventListener('keydown', handleGlobalKeydown);
            window.addEventListener('resize', syncMobileLayout);
        });

        watch([selectedModel], () => {
            saveExpansionSettings(false);
        });

        onUnmounted(() => {
            disconnectSSE();
            if (queueRefreshTimer) {
                clearInterval(queueRefreshTimer);
                queueRefreshTimer = null;
            }
            window.removeEventListener('keydown', handleGlobalKeydown);
            window.removeEventListener('resize', syncMobileLayout);
        });

        // ==================== Return ====================

        return {
            // State
            novels,
            currentNovel,
            currentChapter,
            chapters,
            taskHistory,
            viewMode,
            selectedModel,
            selectedChapterIds,
            chapterRangeInput,
            isExpanding,
            isExpandingCurrent,
            expandTaskId,
            overallProgress,
            completedChapters,
            totalChapters,
            currentExpandingChapter,
            progressLogs,
            hoveredParagraphIndex,
            editingParagraphIndex,
            editingType,
            instructionText,
            streamingParagraphIndex,
            streamingText,
            chapterRewritePromptVisible,
            chapterRewriteInstruction,
            isChapterRewriting,
            chapterEditorVisible,
            chapterEditorTarget,
            chapterEditorText,
            chapterEditorSaving,
            showUploadModal,
            showSettingsModal,
            showExportModal,
            showPromptSettingsModal,
            showDeleteConfirm,
            deleteTarget,
            isDragging,
            isUploading,
            leftCollapsed,
            rightCollapsed,
            isMobileLayout,
            mobileNavCollapsed,
            exportFormat,
            exportSeparatorStyle,
            notifications,
            settings,
            availableModels,
            tokenStatus,

            // API Profiles
            apiProfiles,
            activeProfileId,
            activeProfile,
            editingProfileId,
            editingProfileData,
            // New state
            showExpandConfirm,
            expandEstimate,
            isRetryingFailed,
            interruptedTask,
            failedChaptersCount,
            skippedChaptersCount,
            manualEditIndex,
            manualEditText,
            sseReconnecting,
            queueTasks,
            showChapterTools,
            contentBodyRef,

            // Computed
            sortedQueueTasks,
            canClearTaskHistory,
            displayParagraphs,
            visibleChapters,
            visibleSelectableChapters,
            leftSpacerTop,
            leftSpacerBottom,
            rightSpacerTop,
            rightSpacerBottom,
            originalParagraphs,
            expandedParagraphs,
            hasExpanded,
            allSelected,
            estimatedRemaining,
            progressPercent,
            canUndo,
            currentChapterIndex,
            hasPreviousChapter,
            hasNextChapter,
            failedChapterCount,
            latestFailedTask,
            showFailedTaskAlert,
            tokenWarning,
            startExpandLabel,

            // Methods
            loadNovels,
            selectNovel,
            selectChapter,
            selectRelativeChapter,
            openCatalogFromReader,
            closeMobilePanels,
            showReadingPanel,
            showCatalogPanel,
            showTaskPanel,
            deleteNovel,
            confirmDelete,
            uploadNovel,
            onFileInput,
            onDrop,
            onDragOver,
            onDragLeave,
            startExpand,
            confirmAndStartExpand,
            expandCurrentChapter,
            cancelExpand,
            retryFailed,
            undoExpansion,
            checkInterruptedTask,
            resumeTask,
            dismissInterrupted,
            dismissFailedAlert,
            loadTaskHistory,
            loadQueueTasks,
            clearTaskHistory,
            syncCurrentNovelTaskFromQueue,
            showChapterRewriteInstruction,
            submitChapterRewriteInstruction,
            cancelChapterRewriteInstruction,
            cancelEditing,
            startEditParagraph,
            cancelManualEdit,
            saveManualEdit,
            openChapterEditor,
            closeChapterEditor,
            saveChapterEditor,
            toggleAllChapters,
            toggleChapter,
            isChapterSelected,
            applyChapterRange,
            exportNovel,
            prioritizeTask,
            pauseQueueTask,
            resumeQueueTask,
            cancelTaskById,
            fetchTokenStatus,
            loadProfiles,
            addNewProfile,
            editProfile,
            saveProfile,
            cancelEditProfile,
            deleteProfile,
            switchProfile,
            saveSettings,
            openSettingsModal,
            resetSettings,
            saveExpansionSettings,
            openPromptSettingsModal,
            savePromptSettings,
            resetPromptSettings,
            resetSinglePrompt,
            settingsForm,
            settingsFields,
            settingsGroups,
            settingsActiveTab,
            settingsLoading,
            settingsSaving,
            settingsPasswordVisible,
            promptSettingsLoading,
            promptSettingsSaving,
            promptSettingsActiveGroup,
            promptGroups,
            promptItems,
            promptForm,
            addNotification,
            removeNotification,
            chapterStatusIcon,
            chapterStatusText,
            taskStatusText,
            taskStatusClass,
            taskProgressPercent,
            formatDate,
            formatDuration,
            formatWordCount,
            getParaDisplayText,
            isStreamingPara,
            isParagraphDifferent,
            renderCompareDiff,
            onLeftChapterScroll,
            onRightChapterScroll,
            onChapterRewriteKeydown,
            leftChapterListRef,
            rightCheckboxListRef,
        };
    },
});

app.mount('#app');

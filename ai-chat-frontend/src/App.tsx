import { wanderPresence } from './lib/wanderDisplay';
import { useWanderWidgetPanel } from './lib/useWanderWidgetPanel';
import { useWanderWidgetAvatar } from './lib/useWanderWidgetAvatar';
import { useState, useEffect, useRef, useMemo, useCallback, lazy, Suspense } from 'react';
import './App.css';
import { TaskProgressPanel, TASK_LABELS } from './components/TaskProgressPanel';
import './desktop.css';
import './night-mode.css';
import SettingsNavigation, { type SettingsTab } from './components/SettingsNavigation';
import SocialInbox from './components/SocialInbox';
import {loadSocialFeedPage} from './social/prewarm';
import * as sessionApi from './sessionApi';
import { mapApiMessageToUi } from './lib/msgMapper';
import { useServerMode } from './hooks/useServerMode';
import {createOutgoingDraft, prepareOutgoingMessage, upsertOutgoingMessage, OutgoingPreparationError} from './lib/outgoingMessage';
import { relayMobileToolRequest } from './lib/mobileToolRelay';
import { createChatTurnRecovery, type ChatTurnRecovery } from './lib/chatTurnRecovery';
import { createChatTurnFeedback, type ChatTurnPhase } from './lib/chatTurnFeedback';
import { captureDisplayRows, protectNewerDisplayRows } from './lib/chatDisplayRefresh';
import ChatActivityStatus from './components/ChatActivityStatus';
import {requestMessageResend} from './lib/messageResend';
import type {MessageEditDraft} from './components/MessageEditArea';
import { getCachedHistoryPage, getFullHistoryProgress, invalidateFullHistoryCache,
  prefetchFullHistory, type FullHistoryProgress } from './lib/fullHistoryCache';
import { settleBandReceipts, hiddenReceiptRow, projectToolEventRows } from './lib/chatReceipts';


import { ToastContainer, showToast } from './components/Toast';
import type { ToolCallRecord, Message, VoiceAttachment, ModelType, ImageModelType, Persona, Session, TopicStatus, SummarizedStatus, WanderStatus } from './types';
import MessageRow from './components/MessageRow';
import EventRowShell from './components/EventRowShell';
import MusicHost from './components/music/MusicHost';
import ConversationQuickTools from './components/music/ConversationQuickTools';
import { openMusic, useMusic } from './components/music/api';
import MessageSearchPanel from './components/MessageSearchPanel';
import { PerceptionPanel } from './components/PerceptionPanel';
import TruncationSettings from './components/TruncationSettings';
import { Virtuoso } from 'react-virtuoso';
import type { VirtuosoHandle } from 'react-virtuoso';
import { getApiBase, getMobileBackendCandidates, getWsBase, getPlatform, refreshMobileBackendEndpoint } from './config';



// Lazy-loaded page components (code splitting)
const EventChroniclePage = lazy(() => import('./pages/EventChroniclePage'));
const CognitionPage = lazy(() => import('./pages/CognitionPage'));
const BodyKnowledge = lazy(() => import('./components/BodyKnowledge'));
const MonitorPage = lazy(() => import('./components/MonitorPage'));

const SocialFeedPage = lazy(loadSocialFeedPage);

const LoungePage = lazy(() => import('./pages/LoungePage'));
const MachineLifePage = lazy(() => import('./pages/MachineLifePage'));

const WorkshopPage = lazy(() => import('./pages/WorkshopPage'));

import { FloatingHearts } from './components/FloatingHearts';
import { DraggableCard } from './components/DraggableCard';
import { SessionTitleEditor } from './components/SessionTitleEditor';
import UserStatusPanel from './components/UserStatusPanel';
import UserStatusSettings from './components/UserStatusSettings';
import SettingsGeneralTab from './components/SettingsGeneralTab';
import ApiSettingsTab from './components/ApiSettingsTab';


import SettingsInteractionTab from './components/SettingsInteractionTab';
import SettingsPermissionTab from './components/SettingsPermissionTab';
import ChatInput from './components/ChatInput';
import { useLoungeStatus } from './hooks/useLoungeFocus';


import WanderLogPanel from './components/WanderLogPanel';
import WishBoardModal, { type WishBoardFocus } from './components/WishBoardModal';
import AffectBoard, { AffectQuickCheckin, affectEmoji } from './components/AffectBoard';
import LedgerPanel from './components/LedgerPanel';

import ImportPanel from './components/ImportPanel';
import MemoryView from './components/MemoryView';
import { useChat, MESSAGE_WINDOW } from './context/chat';
import { useBookmark } from './hooks/useBookmark';
import BookmarkPanel from './components/BookmarkPanel';
import { useCalendar } from './hooks/useCalendar';
import CalendarPanel from './components/CalendarPanel';
import ConversationCovenantOverlay from './components/ConversationCovenantOverlay';
import { useConversationCovenant } from './hooks/useConversationCovenant';
import CommerceActionRequests from './components/CommerceActionRequests';
import CommerceSettings from './components/CommerceSettings';
import RiftArchiveSettings from './components/RiftArchiveSettings';
import MobileHubMenu from './components/MobileHubMenu';
import PersonaPage from './components/PersonaPage';

import { prepareAvatarImage } from './lib/avatarImage';


function App() {
  const { sessions, setSessions, currentSessionId, setCurrentSessionId, isLoading, setIsLoading, inputActivities, beginInputActivity, endInputActivity, toolCallStatus, setToolCallStatus, streamingTick, setStreamingTick, currentSessionIdRef, addSession, deleteSession, toggleSessionLock, switchSession, updateSessionMessages, upsertSessionMessage, reloadCurrentSession, recordRemovedMessages, restoreRemovedMessages, reconcileServerMessages } = useChat();
  const bookmark = useBookmark();
  const calendar = useCalendar();
  const sessionsRef = useRef(sessions);
  const covenantBackendSyncRef = useRef<(sessionId: string) => Promise<void>>(async () => {});
  const covenant = useConversationCovenant((sessionId) => covenantBackendSyncRef.current(sessionId));
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);
  const [isLoadingSessions, setIsLoadingSessions] = useState(true);
  const [sessionsReload, setSessionsReload] = useState(0);

  
  
  
  
  
  // ── 后宫群聊 ──
  
  
  
  // 后宫活跃时轮询兜底：WS 漏掉的慢回复（如 Codex 长延迟）从 API 补拉
  
  const [model] = useState<ModelType>('deepseek');
  const [imageModel, setImageModel] = useState<ImageModelType>('flash-vision');
  const [useMemory] = useState(true);
  const nightControl = useServerMode('nightMode', '/api/night-mode', 'night_mode', '深夜模式');
  const nightMode = nightControl.value;
  const { data: musicState, error: musicDisconnected } = useMusic();
  const musicMode = !musicDisconnected && ['playing', 'paused'].includes(musicState?.session?.status || '');
  const phoneBrowseControl = useServerMode('phoneBrowseMode', '/api/phone-browse/mode', 'phone_browse_mode', '刷手机模式');
  const phoneBrowseMode = phoneBrowseControl.value;
  const [phoneBrowseInterval, setPhoneBrowseInterval] = useState(() => {
    const saved = localStorage.getItem('phoneBrowseInterval');
    return saved ? parseInt(saved) : 5;
  });
  useEffect(() => {
    localStorage.setItem('phoneBrowseInterval', String(phoneBrowseInterval));
  }, [phoneBrowseInterval]);
  // 悬浮球显隐：phoneBrowseMode 变化时发指令；回到前台时也重试（权限授权后 / 后台恢复）
  // 悬浮球状态诊断（显示在设置面板里）
  const [widgetDiag, setWidgetDiag] = useState('⚪ 未操作');
  // 开机自启状态
  const [autoStart, setAutoStart] = useState<{ mirrow: boolean }>({ mirrow: true });
  // 自动语音朗读
  const [autoPlayVoice, setAutoPlayVoice] = useState(() => {
    try { return localStorage.getItem('autoPlayVoice') === 'true'; } catch { return false; }
  });
  const toggleAutoPlayVoice = useCallback(() => {
    setAutoPlayVoice(prev => {
      const next = !prev;
      try { localStorage.setItem('autoPlayVoice', String(next)); } catch {}
      return next;
    });
  }, []);
  const [autoStartLoaded, setAutoStartLoaded] = useState(false);
  const fetchAutoStartStatus = useCallback(async () => {
    try {
      const res = await fetch(`${getApiBase()}/api/auto-start/status`);
      const data = await res.json();
      setAutoStart({ mirrow: data.mirrow?.enabled ?? true });
      setAutoStartLoaded(true);
    } catch { setAutoStartLoaded(true); }
  }, []);
  const tryShowWidget = useCallback(() => {
    const capWin = window as any;
    if (!capWin.Capacitor?.Plugins?.MobileTool) {
      setWidgetDiag('❌ Capacitor 插件未加载');
      return;
    }
    setWidgetDiag('⏳ 正在发送...');
    capWin.Capacitor.Plugins.MobileTool.execute({ tool: 'show_floating_widget', params: JSON.stringify({}) })
      .then((r: any) => {
        const p = typeof r === 'string' ? JSON.parse(r) : r;
        if (p?.success) {
          setWidgetDiag('✅ 悬浮球已显示');
        } else {
          setWidgetDiag('❌ ' + (p?.content || '未知错误'));
        }
      })
      .catch((e: any) => setWidgetDiag('❌ 异常: ' + (e?.message || e)));
  }, []);

  useEffect(() => {
    if (getPlatform() !== 'mobile') return;
    const capWin = window as any;
    if (!capWin.Capacitor?.Plugins?.MobileTool) return;

    if (phoneBrowseMode) {
      tryShowWidget();
      // 每当回到前台且模式开着，就重试显示（覆盖：权限授权后返回 / 后台恢复 / 重启恢复）
      const onVisible = () => {
        if (document.visibilityState === 'visible') {
          console.log('[PHONE_BROWSE] 回到前台，重试显示悬浮球');
          tryShowWidget();
        }
      };
      document.addEventListener('visibilitychange', onVisible);
      return () => document.removeEventListener('visibilitychange', onVisible);
    } else {
      console.log('[PHONE_BROWSE] hide_floating_widget');
      capWin.Capacitor.Plugins.MobileTool.execute({ tool: 'hide_floating_widget', params: JSON.stringify({}) })
        .catch(() => {});
    }
  }, [phoneBrowseMode, tryShowWidget]);

  // 启动时将固定候选端点和本次选中的地址交给原生后台 Relay。
  useEffect(() => {
    if (getPlatform() !== 'mobile') return;
    const capWin = window as any;
    const plugin = capWin.Capacitor?.Plugins?.MobileTool;
    if (!plugin) return;

    const apiBase = getApiBase();
    console.log('[PHONE_BROWSE] 已选择固定后端端点');
    plugin.execute({
      tool: 'write_backend_endpoints',
      params: JSON.stringify({
        selected_url: apiBase,
        candidates: getMobileBackendCandidates(),
      }),
    }).catch((e: any) => console.warn('[PHONE_BROWSE] write_backend_endpoints 失败:', e));
  }, []);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [longTextCardFullContext, setLongTextCardFullContext] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('general');
  const [view, setView] = useState<'chat' | 'persona' | 'monitor' | 'memory' | 'events' | 'social' | 'machineLife' | 'workshop' | 'lounge' | 'worldbook' | 'selfbook'>('chat');
  const [workshopJobId, setWorkshopJobId] = useState('');
  const [lifeChild, setLifeChild] = useState(false);
  
  
  // 设置面板打开时刷新自启状态
  useEffect(() => { if (settingsOpen) { fetchAutoStartStatus(); fetchSettings(); } }, [settingsOpen, fetchAutoStartStatus]);
  // 图像分析模型现由后端 .env(IMAGE_MODEL) 控制，前端仅同步显示
  useEffect(() => {
    fetch(`${getApiBase()}/api/config/image-model`).then(r => r.json())
      .then(d => { if (d.image_model === 'glm' || d.image_model === 'deepseek' || d.image_model === 'flash-vision') setImageModel(d.image_model); })
      .catch(() => {});
  }, []);
  // 切换页面时关闭所有独立浮层，防止残留遮挡
  useEffect(() => {
    setShowAffectBoard(false);
    setShowAffectQuickCheckin(false);
    setShowLedger(false);
    setShowBookmark(false);
    setShowWanderLog(false);
    setShowSentinel(false);
    setShowBrainArch(false);
    setShowSleepPrompt(false);
    setSettingsOpen(false);
    calendar.setTaskPanelOpen(false);
  }, [view]);
  // "聊到这里"后异步任务进度
  const [taskProgress, setTaskProgress] = useState<{
    visible: boolean;
    tasks: Record<string, { status: string; name?: string; message?: string; stalled?: boolean }>;
    allDone: boolean;
    progress: number;
  }>({ visible: false, tasks: {}, allDone: true, progress: 0 });
  // 卡住弹窗（任务连续 5min 无进展时弹出）
  const [stallModal, setStallModal] = useState<{ taskId: string; label: string } | null>(null);
  // 用户选「继续等待」后抑制重复弹窗的任务集（ref 避免 setInterval 闭包读到旧值）
  const dismissedStallsRef = useRef<Set<string>>(new Set());
  // 任务进度轮询定时器（抽出复用，防重复启动）
  const taskPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 任务标签映射（进度条 + 弹窗共用）
  const personaLoadedRef = useRef(false);
  const personaSaveSequenceRef = useRef(0);
  const personaSaveChainRef = useRef<Promise<void>>(Promise.resolve());
  const [personaSyncStatus, setPersonaSyncStatus] = useState<'saved' | 'saving' | 'error'>('saved');
  const chatBackgroundOpacityMigratedRef = useRef(localStorage.getItem('mirrow_chat_bg_opacity_v2') === '1');
  const userAvatarLoadedRef = useRef(false);
  const [persona, setPersona] = useState<Persona>(() => {
    const saved = localStorage.getItem('aiPersona');
    if (saved) {
      try {
        const p = JSON.parse(saved);
        // 本地缓存只负责离线展示，不能在服务端读回前获得写权限；否则另一台
        // 设备的旧缓存会在启动瞬间覆盖刚保存的新头像。
        return p;
      } catch (e) {
        console.error('Failed to parse saved persona', e);
      }
    }
    return {
      name: 'AI助手',
      avatar: '🤖',
      nickname: '',
      systemPrompt: '你是一个乐于助人的AI助手。'
    };
  });
  const chatBackgroundSrc = persona.chatBackground?.startsWith('/')
    ? `${getApiBase()}${persona.chatBackground}`
    : persona.chatBackground;

  // 旧版刚上线时默认仅 24%，在有背景的设备上一次性提升到新的 42%。
  // 迁移完成后不再干预，人类伙伴仍可手动选择包括 24% 在内的任意值。
  useEffect(() => {
    if (chatBackgroundOpacityMigratedRef.current || !persona.chatBackground) return;
    chatBackgroundOpacityMigratedRef.current = true;
    localStorage.setItem('mirrow_chat_bg_opacity_v2', '1');
    if (persona.chatBackgroundOpacity == null || Math.abs(persona.chatBackgroundOpacity - 0.24) < 0.001) {
      setPersona(current => ({ ...current, chatBackgroundOpacity: 0.42 }));
    }
  }, [persona.chatBackground, persona.chatBackgroundOpacity]);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [expandedThinkingId, setExpandedThinkingId] = useState<string | null>(null);
  const [showWishBoard, setShowWishBoard] = useState(false);
  const [wishBoardFocus, setWishBoardFocus] = useState<WishBoardFocus | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const editingDraftRef = useRef<MessageEditDraft | null>(null);
  const resendInFlightRef = useRef<Set<string>>(new Set());
  const mainSendInFlightRef = useRef(false);
  const activeChatRecoveryRef = useRef<ChatTurnRecovery | null>(null);
  const [chatTurnPhase, setChatTurnPhase] = useState<ChatTurnPhase>('idle');
  const [chatTurnFeedback] = useState(() => createChatTurnFeedback(setChatTurnPhase));
  const [resendingMessageIds, setResendingMessageIds] = useState<Set<string>>(new Set());
  const messageMutationInFlightRef = useRef<Set<string>>(new Set());
  const [mutatingMessageIds, setMutatingMessageIds] = useState<Set<string>>(new Set());
  const voiceActivityTokenRef = useRef<string | null>(null);
  const activeInputActivities = Object.values(inputActivities);
  const inputBlocked = isLoading || activeInputActivities.length > 0;
  
  const executingTools = toolCallStatus.filter(call => call.status !== 'complete');
  const inputActivityLabel = activeInputActivities.find(activity => activity.kind === 'voice_recognition')?.label
    || (chatTurnPhase === 'saving' ? '正在保存本轮结果…' : '')
    || (chatTurnPhase === 'recovering' ? '正在确认回复状态…' : '')
    || activeInputActivities.find(activity => activity.kind === 'send_preparation')?.label
    || (executingTools.length ? executingTools.map(call => call.description || '正在执行操作…').join(' · ') : '')
    || (toolCallStatus.length > 0 && toolCallStatus.every(call => call.status === 'complete')
        ? '正在结合工具结果…'
      : '正在回复…');
  const handleVoiceProcessingChange = useCallback((active: boolean) => {
    if (active && !voiceActivityTokenRef.current) {
      voiceActivityTokenRef.current = beginInputActivity('voice_recognition', '正在识别语音…');
    } else if (!active && voiceActivityTokenRef.current) {
      endInputActivity(voiceActivityTokenRef.current);
      voiceActivityTokenRef.current = null;
    }
  }, [beginInputActivity, endInputActivity]);
  const [userAvatar, setUserAvatar] = useState<string>(() => {
    return localStorage.getItem('userAvatar') || '👤';
  });
  const [topicStatuses, setTopicStatuses] = useState<Record<string, TopicStatus>>({});
  const [showLedger, setShowLedger] = useState(false);
  
  const [showMobileSidebar, setShowMobileSidebar] = useState(false);
  const [showMobileMoreMenu, setShowMobileMoreMenu] = useState(false);
  const [a11yStatus, setA11yStatus] = useState('未检测');
  const [showWanderLog, setShowWanderLog] = useState(false);
  // 漫想日志 state 已移至 hooks/useWanderLog.ts
  const [moodDisplay, setMoodDisplay] = useState<{emoji:string;label:string;text:string}>({emoji:'🌤️',label:'暂时没有主心情',text:''});
  const [showMoodPeek, setShowMoodPeek] = useState(false);
  const [showAffectBoard, setShowAffectBoard] = useState(false);
  useWanderWidgetAvatar(persona.avatar);
  useWanderWidgetPanel(view, setView, setShowAffectBoard, setShowWanderLog, setShowMobileSidebar);
  const [showAffectQuickCheckin, setShowAffectQuickCheckin] = useState(false);
  const [showBrainArch, setShowBrainArch] = useState(false);

  const openWishBoard = useCallback((metadata?: Record<string, any>) => {
    setWishBoardFocus(metadata && (metadata.target_wish_id ?? metadata.wish_id)
      ? metadata as WishBoardFocus
      : null);
    setShowWishBoard(true);
  }, []);

  const [chatView, setChatView] = useState<'normal' >('normal');
  
  const fetchBrainArchitecture = () => setShowBrainArch(true);
  const [showBookmark, setShowBookmark] = useState(false);
  const [showPersonaEvo, setShowPersonaEvo] = useState(false);
  // 人格演化 state 已移至 hooks/usePersonaEvo.ts
  const [showSentinel, setShowSentinel] = useState(false);
  // 导入功能状态
  const [showImport, setShowImport] = useState(false);
  
  // 睡眠提示 + 上传
  const [showSleepPrompt, setShowSleepPrompt] = useState(false);
  const [sleepUploading, setSleepUploading] = useState(false);
  // 睡眠数据上传
  const handleSleepUpload = async (file: File) => {
    setSleepUploading(true);
    try {
      const reader = new FileReader();
      const base64 = await new Promise<string>((resolve, reject) => {
        reader.onload = () => resolve((reader.result as string).split(',')[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const res = await fetch(`${getApiBase()}/api/health/upload-sleep-image`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: base64, date: new Date().toISOString().slice(0, 10) }),
      });
      const data = await res.json();
      if (data.success) {
        setShowSleepPrompt(false);
        alert(`睡眠数据已记录：${data.data.total_sleep_min}分钟`);
      } else {
        alert(data.error || '上传失败');
      }
    } catch (e: any) {
      alert('上传失败: ' + e.message);
    } finally {
      setSleepUploading(false);
    }
  };
  const handleSleepSkip = async () => {
    setShowSleepPrompt(false);
    try {
      await fetch(`${getApiBase()}/api/health/sleep-data-status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'skipped' }),
      });
    } catch {}
  };
  const handleOpenHuaweiHealth = async () => {
    try {
      await (window as any).Capacitor?.Plugins?.MobileTool?.execute({tool: 'mobile_open_health_app'});
    } catch {
      window.open('intent://#Intent;package=com.huawei.health;end', '_blank');
    }
  };

  const fetchSettings = async () => {
    const personaSequenceAtRead=personaSaveSequenceRef.current;
    try {
      const res = await fetch(`${getApiBase()}/api/settings`, {cache:'no-store'});
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.aiPersona && typeof data.aiPersona === 'object' && personaSaveSequenceRef.current===personaSequenceAtRead) {
        const pendingAiAvatar = localStorage.getItem('mirrow_pending_ai_avatar');
        // 旧后端可能尚未保存聊天背景；保留本地最后一次选择并立即同步，
        // 不能让一个“缺字段”的权威对象把已经确认的视觉设置清空。
        const mergedPersona = {
          ...data.aiPersona,
          ...(data.aiPersona.chatBackground == null && persona.chatBackground
            ? {
                chatBackground: persona.chatBackground,
                chatBackgroundOpacity: persona.chatBackgroundOpacity ?? 0.42,
              }
            : {}),
          ...(pendingAiAvatar ? { avatar: pendingAiAvatar } : {}),
        };
        personaLoadedRef.current = true;
        setPersona(mergedPersona);
        localStorage.setItem('aiPersona', JSON.stringify(mergedPersona));
      }
      if (data.userAvatar && typeof data.userAvatar === 'string') {
        const pendingUserAvatar = localStorage.getItem('mirrow_pending_user_avatar');
        userAvatarLoadedRef.current = true;
        setUserAvatar(pendingUserAvatar || data.userAvatar);
        localStorage.setItem('userAvatar', pendingUserAvatar || data.userAvatar);
      }
      if (data.user_status_mode === 'manual') {
        setUserStatusMode('manual');
      }
      setLongTextCardFullContext(Boolean(data.longTextCardFullContext));
    } catch { /* ignore */ }
  };
  const fetchMood = async () => {
    try {
      const res = await fetch(`${getApiBase()}/api/affect/projection`, { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok || !data?.self) return;
      const dominant = data.self.dominant || {};
      const label = dominant.label || '暂时没有主心情';
      setMoodDisplay({ emoji: affectEmoji(dominant.family, dominant.label), label, text: data.renderer_self || data.renderer_resonance || '' });
    } catch { /* ignore */ }
  };
  
  
  useEffect(() => { if(view==='persona')fetchSettings(); }, [view]);
  useEffect(() => {
    fetchSettings();
    const iv = setInterval(fetchMood, 30 * 1000);
    window.addEventListener('focus', fetchMood);
    return () => { clearInterval(iv); window.removeEventListener('focus', fetchMood); };
  }, []);
  useEffect(() => { if (!isLoading) void fetchMood(); }, [isLoading]);
  // 人格演化逻辑已移至 hooks/usePersonaEvo.ts
  const [selectionFlag, setSelectionFlag] = useState<{text: string; x: number; y: number} | null>(null);
  const [flagSubmitted, setFlagSubmitted] = useState(false);
  // 漫想日志逻辑已移至 hooks/useWanderLog.ts
  const [wanderStatus, setWanderStatus] = useState<WanderStatus | null>(null);
  const loungeStatus = useLoungeStatus();
  const presence = wanderPresence(wanderStatus);
  const [userStatus, setUserStatus] = useState<string>('');
  const [userStatusMode, setUserStatusMode] = useState<string>('auto');
  const [statusMeta, setStatusMeta] = useState<any>(null);  // StatusMeta dict from backend
  const [summarizedStatus, setSummarizedStatus] = useState<SummarizedStatus | null>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const messagesRef = useRef<HTMLDivElement>(null);
  const wanderWsRef = useRef<WebSocket | null>(null);
  const musicWsRef = useRef<WebSocket | null>(null);
  const wanderAddedMsgIds = useRef<Set<string>>(new Set());  // 防止 React Strict Mode 重复添加漫想消息
  const prevExpandedIdRef = useRef<string | null>(null);  // 跟踪收起前的思考展开消息ID
  const isLoadingRef = useRef(isLoading);    // 供 visibility handler 等回调读取最新值
  const primarySessionId = sessions.find(session => session.isPrimary)?.id || '';
  const [fullHistoryProgress, setFullHistoryProgress] = useState<FullHistoryProgress | null>(null);
  const [prefetchingSessionId, setPrefetchingSessionId] = useState('');
  const fullHistoryAbortRef = useRef<AbortController | null>(null);
  const silentHistoryAbortRef = useRef<AbortController | null>(null);
  const lastHistoryProgressPaintRef = useRef(0);
  const recoveryInProgressRef = useRef(false);  // 防止重复启动恢复轮询
  const reloadSessionRef = useRef<() => void>(() => {});
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const hideScrollToBottomTimerRef = useRef<number | null>(null);
  const [pendingWanderMessages, setPendingWanderMessages] = useState<Message[]>([]);
  const [pendingSessionSwitch, setPendingSessionSwitch] = useState<string | null>(null);
  const pendingLatestScrollRef = useRef(false);
  const [latestScrollRequest, setLatestScrollRequest] = useState(0);

  useEffect(() => {
    if (!primarySessionId) return;
    const controller = new AbortController();
    const refreshCount = async () => {
      try {
        const count = await sessionApi.getSessionMessageCount(primarySessionId, controller.signal);
        if (controller.signal.aborted) return;
        setSessions(prev => prev.map(session => session.id === primarySessionId
          ? { ...session, totalMessages: count.total } : session));
        const cached = await getFullHistoryProgress(primarySessionId).catch(() => null);
        if (!controller.signal.aborted && !fullHistoryAbortRef.current) {
          setFullHistoryProgress(cached?.revision === count.updatedAt && cached.total === count.total
            ? cached : null);
        }
      } catch { /* 下次刷新或手动预读时重试 */ }
    };
    void refreshCount();
    const interval = window.setInterval(() => {
      if (!document.hidden) void refreshCount();
    }, 60_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [primarySessionId]);

  const toggleFullHistoryPrefetch = (sessionId: string) => {
    if (prefetchingSessionId === sessionId) {
      fullHistoryAbortRef.current?.abort();
      return;
    }
    if (fullHistoryAbortRef.current) fullHistoryAbortRef.current.abort();
    const controller = new AbortController();
    fullHistoryAbortRef.current = controller;
    setPrefetchingSessionId(sessionId);
    lastHistoryProgressPaintRef.current = 0;
    void prefetchFullHistory(sessionId, progress => {
      const now = Date.now();
      if (!controller.signal.aborted && (progress.complete || now - lastHistoryProgressPaintRef.current >= 2000)) {
        lastHistoryProgressPaintRef.current = now;
        setFullHistoryProgress(progress);
      }
    }, controller.signal, () => isLoadingRef.current || document.hidden).then(progress => {
      setSessions(prev => prev.map(session => session.id === sessionId
        ? { ...session, totalMessages: progress.total } : session));
      showToast(`本机已缓存 ${progress.loaded} 条消息`);
    }).catch(error => {
      if (controller.signal.aborted) {
        if (silentHistoryAbortRef.current !== controller) showToast('已暂停全量预读，可再次点击续载');
      }
      else showToast(error instanceof Error ? error.message : '全量预读失败');
    }).finally(() => {
      if (fullHistoryAbortRef.current === controller) {
        fullHistoryAbortRef.current = null;
        setPrefetchingSessionId('');
      }
      if (silentHistoryAbortRef.current === controller) silentHistoryAbortRef.current = null;
    });
  };

  const invalidateLocalHistory = (sessionId: string) => {
    if (sessionId !== primarySessionId) return;
    if (fullHistoryAbortRef.current) {
      silentHistoryAbortRef.current = fullHistoryAbortRef.current;
      fullHistoryAbortRef.current.abort();
    }
    setFullHistoryProgress(null);
    void invalidateFullHistoryCache(sessionId);
  };
  // 流式输出轻量刷新：+1 触发重渲染但不改变 visibleMessages 引用 → Virtuoso 不重测高度

  const scrollToBottom = () => {
    
    pendingLatestScrollRef.current = true;
    setHistoryView(null);
    setDisplayCount(MESSAGE_WINDOW);
    setShowScrollToBottom(false);
    setLatestScrollRequest(request => request + 1);
  };


  // 会话管理辅助函数
  const getCurrentSession = () => {
    return sessions.find(session => session.id === currentSessionId);
  };

  const getCurrentMessages = () => {
    const session = getCurrentSession();
    return session ? session.messages : [];
  };

  const [displayCount, setDisplayCount] = useState(MESSAGE_WINDOW);
  const [historyView, setHistoryView] = useState<{
    startId: string; startTs: string; startRowId?: number;
    endId: string; endTs: string; endRowId?: number;
    hasMoreBefore: boolean; hasMoreAfter: boolean;
  } | null>(null);
  const loadingMoreRef = useRef(false);
  const loadingNewerRef = useRef(false);
  const [loadingNewer, setLoadingNewer] = useState(false);
  // 向上翻页后要钉回视口顶部的消息 id（见下方 anchor effect）
  const pendingScrollAnchorRef = useRef<string | null>(null);

  // ── 消息搜索 + 定位 ──
  const [showMsgSearch, setShowMsgSearch] = useState(false);
  const [highlightedMsgId, setHighlightedMsgId] = useState<string | null>(null);
  const pendingLocateIdRef = useRef<string | null>(null);
  
  const highlightTimerRef = useRef<number | null>(null);

  // 切换会话时重置显示数量 + 搜索/定位状态
  useEffect(() => {
    setDisplayCount(MESSAGE_WINDOW);
    setHistoryView(null);
    setShowMsgSearch(false);
    setHighlightedMsgId(null);
    pendingLocateIdRef.current = null;
    pendingScrollAnchorRef.current = null;
  }, [currentSessionId]);

  const dedupedMessages = useMemo(() => {
    const all = getCurrentMessages();
    // 按 ID 去重：同一 ID 的消息只保留最后一次出现
    // 解决 WebSocket 实时 llm_reply 和 session 持久化 im_ 消息共用同一 ID 时的重复渲染
    const seen = new Map<string, number>();
    all.forEach((m, i) => { if (m.id) seen.set(m.id, i); });
    return all.filter((m, i) => (!m.id || seen.get(m.id) === i)
      && (m.id === editingMessageId || !hiddenReceiptRow(m)));
  }, [currentSessionId, sessions, editingMessageId]);

  const visibleMessages = useMemo(() => {
    if (historyView) {
      const start = dedupedMessages.findIndex(m => m.id === historyView.startId);
      const end = dedupedMessages.findIndex(m => m.id === historyView.endId);
      return start >= 0 && end >= start ? dedupedMessages.slice(start, end + 1) : [];
    }
    const tailStartId = getCurrentSession()?.tailStartId;
    const start = tailStartId ? dedupedMessages.findIndex(m => m.id === tailStartId) : -1;
    const contiguousTail = start >= 0 ? dedupedMessages.slice(start) : dedupedMessages;
    return contiguousTail.slice(-displayCount);
  }, [dedupedMessages, displayCount, historyView, currentSessionId, sessions]);
  const timelineMessages = useMemo(() => projectToolEventRows(visibleMessages), [visibleMessages]);

  useEffect(() => {
    if (!pendingLatestScrollRef.current || view !== 'chat' || chatView !== 'normal'
      || historyView || !timelineMessages.length) return;
    let frame = 0;
    let attempts = 0;
    const scrollWhenReady = () => {
      const scroller = messagesRef.current?.querySelector<HTMLElement>('[data-test-id="virtuoso-scroller"], [data-virtuoso-scroller]');
      if (scroller && scroller.clientHeight > 0) {
        virtuosoRef.current?.scrollToIndex({
          index: timelineMessages.length - 1, align: 'end', behavior: 'auto',
        });
        scroller.scrollTop = scroller.scrollHeight;
        if (attempts >= 5 && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 2) {
          pendingLatestScrollRef.current = false;
          return;
        }
      }
      if (++attempts < 20) frame = window.requestAnimationFrame(scrollWhenReady);
    };
    frame = window.requestAnimationFrame(scrollWhenReady);
    return () => window.cancelAnimationFrame(frame);
  }, [view, chatView, historyView, timelineMessages, latestScrollRequest]);

  useEffect(() => () => {
    if (hideScrollToBottomTimerRef.current !== null) window.clearTimeout(hideScrollToBottomTimerRef.current);
  }, []);

  const handleAtBottomChange = (atBottom: boolean) => {
    if (hideScrollToBottomTimerRef.current !== null) window.clearTimeout(hideScrollToBottomTimerRef.current);
    if (!atBottom) {
      setShowScrollToBottom(true);
      return;
    }
    // 搜索弹层和虚拟列表重测高度会短暂报告触底，避免导航按钮闪烁。
    hideScrollToBottomTimerRef.current = window.setTimeout(() => setShowScrollToBottom(false), 250);
  };

  const hiddenCount = useMemo(() => {
    if (historyView) return 0;
    const startId = getCurrentSession()?.tailStartId;
    const start = startId ? dedupedMessages.findIndex(m => m.id === startId) : -1;
    return Math.max(0, (start >= 0 ? dedupedMessages.length - start : dedupedMessages.length) - displayCount);
  }, [currentSessionId, displayCount, sessions, dedupedMessages, historyView]);

  // 向上翻页：按游标向服务端要更早的一页，而不是一次拉全量。
  // 游标 = 当前已加载消息里最早的一条；本地生成的通知类消息服务端没有，跳过。
  const loadMoreMessages = async () => {
    const sid = currentSessionIdRef.current;
    const loaded = getCurrentMessages();
    const anchorId = historyView?.startId || getCurrentSession()?.tailStartId;
    const anchor = loaded.find(m => m.id === anchorId) || loaded.find(m => m.role !== 'notification');
    const serverHasMore = historyView?.hasMoreBefore ?? getCurrentSession()?.hasMoreHistory ?? false;

    if (sid && anchorId && serverHasMore && !loadingMoreRef.current) {
      loadingMoreRef.current = true;
      try {
        const session = await getCachedHistoryPage(sid, anchorId) || await sessionApi.getSession(sid, {
          limit: MESSAGE_WINDOW,
          beforeId: anchorId,
          beforeTs: historyView?.startTs || getCurrentSession()?.tailStartTs,
          beforeRowId: historyView?.startRowId || getCurrentSession()?.tailStartRowId,
        });
        if (!session) throw new Error('历史消息请求失败');
        if (session) {
          if (session.cursorStale) {
            showToast('历史位置已变化，请重新搜索或翻页');
            return;
          }
          const backendMsgs = session.messages.map((apiMsg: any) => mapApiMessageToUi(apiMsg));
          setSessions(prev => prev.map(s => {
            if (s.id !== sid) return s;
            return {
              ...s,
              // 翻页是显式动作：并集插入，绝不走删除分支
              messages: reconcileServerMessages(sid, s.messages || [], backendMsgs, true, true),
              hasMoreHistory: historyView ? s.hasMoreHistory : (session.hasMore ?? false),
              tailStartId: historyView ? s.tailStartId : (backendMsgs[0]?.id || s.tailStartId),
              tailStartTs: historyView ? s.tailStartTs : (session.oldestTs || s.tailStartTs),
              tailStartRowId: historyView ? s.tailStartRowId : (session.oldestRowId || s.tailStartRowId),
              updatedAt: new Date(),
            };
          }));
          if (backendMsgs.length > 0) {
            if (historyView) setHistoryView(view => view && view.startId === anchorId
              ? { ...view, startId: backendMsgs[0].id, startTs: session.oldestTs || view.startTs,
                  startRowId: session.oldestRowId || view.startRowId,
                  hasMoreBefore: session.hasMore ?? false } : view);
            // 记下原视口顶部那条，稍后把它钉回顶部（见下方 anchor effect）
            if (anchor) pendingScrollAnchorRef.current = anchor.id;
          }
        }
      } catch { showToast('加载更早消息失败，请重试'); }
      finally { loadingMoreRef.current = false; }
    }
    // 渲染窗口同步放大；超出的部分 slice(-N) 自然取全部，不会溢出
    if (!historyView) setDisplayCount(c => c + MESSAGE_WINDOW);
  };

  const loadNewerMessages = async () => {
    const view = historyView;
    const sid = currentSessionIdRef.current;
    if (!view?.hasMoreAfter || !sid || loadingNewerRef.current) return;
    loadingNewerRef.current = true;
    setLoadingNewer(true);
    try {
      const page = await sessionApi.getMessagesAfter(sid, view.endId, view.endTs, view.endRowId);
      if (currentSessionIdRef.current !== sid) return;
      if (page.cursorStale) { showToast('历史位置已变化，请重新搜索定位'); return; }
      const messages = page.messages.map(mapApiMessageToUi);
      if (messages.length) {
        setSessions(prev => prev.map(s => s.id === sid ? {
          ...s, messages: reconcileServerMessages(sid, s.messages, messages, true, true),
        } : s));
      }
      setHistoryView(current => current && current.endId === view.endId ? {
        ...current,
        endId: page.newestId || current.endId,
        endTs: page.newestTs || current.endTs,
        endRowId: page.newestRowId || current.endRowId,
        hasMoreAfter: page.hasMore,
      } : current);
    } catch { showToast('加载较新消息失败，请重试'); }
    finally { loadingNewerRef.current = false; setLoadingNewer(false); }
  };

  // ── 消息定位：闪烁高亮 2.5s 后自动清除 ──
  const flashHighlight = (msgId: string) => {
    if (highlightTimerRef.current) window.clearTimeout(highlightTimerRef.current);
    setHighlightedMsgId(msgId);
    highlightTimerRef.current = window.setTimeout(() => setHighlightedMsgId(null), 2500);
  };

  // 消息定位：当前窗口内直接滚动；窗口外向服务端取得目标周围的一段消息。
  const locateMessage = async (msgId: string) => {
    if (isLoading) return; // rAF 追底循环独占滚动，不争抢（trap 78）
    setShowMsgSearch(false);
    const sid = currentSessionIdRef.current;
    const idx = visibleMessages.findIndex(m => m.id === msgId);
    if (idx === -1) {
      try {
        const window = await sessionApi.getMessagesAround(sid, msgId);
        if (currentSessionIdRef.current !== sid) return;
        const messages = window.messages.map(mapApiMessageToUi);
        if (!messages.length) return;
        setSessions(prev => prev.map(s => s.id === sid ? {
          ...s, messages: reconcileServerMessages(sid, s.messages, messages, true, true),
        } : s));
        setHistoryView({ startId: messages[0].id, startTs: window.messages[0].timestamp,
          startRowId: messages[0].rowOrder,
          endId: messages[messages.length - 1].id,
          endTs: window.messages[window.messages.length - 1].timestamp,
          endRowId: messages[messages.length - 1].rowOrder,
          hasMoreBefore: window.hasMoreBefore, hasMoreAfter: window.hasMoreAfter });
        pendingLocateIdRef.current = msgId;
      } catch { showToast('原消息已经不存在或暂时无法定位'); }
      return;
    }
    setShowMsgSearch(false);
    const vIdx = timelineMessages.findIndex(m => m.id === msgId);
    if (vIdx === -1) return;
    virtuosoRef.current?.scrollToIndex({ index: vIdx, align: 'center', behavior: 'auto' });
    flashHighlight(msgId);
  };

  // 服务端窗口进入虚拟列表后再定位目标。
  useEffect(() => {
    const target = pendingLocateIdRef.current;
    if (!target) return;
    const vIdx = timelineMessages.findIndex(m => m.id === target);
    if (vIdx === -1) return;
    pendingLocateIdRef.current = null;
    // 50ms 让 Virtuoso 先消化新窗口的数据。
    setTimeout(() => {
      virtuosoRef.current?.scrollToIndex({ index: vIdx, align: 'center', behavior: 'auto' });
    }, 50);
    flashHighlight(target);
  }, [timelineMessages]);

  // 向上翻页后把原视口顶部那条钉回顶部，避免 Virtuoso 因头部插入而跳位
  useEffect(() => {
    const anchorId = pendingScrollAnchorRef.current;
    if (!anchorId) return;
    const vIdx = timelineMessages.findIndex(m => m.id === anchorId);
    if (vIdx === -1) return;
    pendingScrollAnchorRef.current = null;
    setTimeout(() => {
      virtuosoRef.current?.scrollToIndex({ index: vIdx, align: 'start', behavior: 'auto' });
    }, 50);
  }, [timelineMessages]);

  

  // 相册定位复用消息 ID 的服务端周围窗口，旧照片不要求先加载整段历史。
  

  // 新消息滚动由 Virtuoso followOutput 自动处理，不再手动触发



  const editSessionTitle = async (sessionId: string, newTitle: string) => {
    if (!newTitle.trim()) return;

    const session = sessions.find(s => s.id === sessionId);
    if (!session) return;

    const updatedSession = {
      ...session,
      title: newTitle.trim(),
      updatedAt: new Date()
    };

    try {
      // 更新到后端，只更新标题
      await sessionApi.updateSession(sessionId, { title: newTitle.trim() });

      // 更新本地状态
      setSessions(prev => prev.map(s =>
        s.id === sessionId ? updatedSession : s
      ));

      setEditingSessionId(null);
    } catch (error) {
      console.error('更新会话标题失败:', error);
      // 仍然更新本地状态
      setSessions(prev => prev.map(s =>
        s.id === sessionId ? updatedSession : s
      ));
      setEditingSessionId(null);
    }
  };

  // 加载会话数据
  useEffect(() => {
    const abortController = new AbortController();

    async function loadSessions() {
      const MAX_RETRIES = 5;
      const RETRY_DELAY_MS = 2000;

      const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
      const platform = getPlatform();
      const apiBase = getApiBase();
      console.log(`[loadSessions] ═══ START platform=${platform} apiBase=${apiBase} ═══`);

      for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
        try {
          setIsLoadingSessions(true);
          const isMobile = platform === 'mobile';
          console.log(`[loadSessions] attempt=${attempt + 1}/${MAX_RETRIES} mobile=${isMobile} api=${apiBase}/api/sessions`);
          const response = await sessionApi.getAllSessions(abortController.signal, 200);  // 初始加载最近200条，翻旧消息时按需加载

          // 转换API会话到前端会话格式
          const frontendSessions: Session[] = response.sessions.map(apiSession => ({
            id: apiSession.id,
            title: apiSession.title,
            messages: apiSession.messages.map(apiMsg => mapApiMessageToUi(apiMsg)),
            createdAt: new Date(apiSession.createdAt),
            updatedAt: new Date(apiSession.updatedAt),
            locked: apiSession.locked || false,
            isPrimary: apiSession.isPrimary || false,
            hasMoreHistory: apiSession.hasMore ?? false,
            tailStartId: apiSession.oldestId || undefined,
            tailStartTs: apiSession.oldestTs || undefined,
            tailStartRowId: apiSession.oldestRowId || undefined,
          }));

          setSessions(prev => frontendSessions.map(session => ({
            ...session,
            totalMessages: prev.find(item => item.id === session.id)?.totalMessages,
            messages: reconcileServerMessages(
              session.id,
              prev.find(item => item.id === session.id)?.messages || [],
              session.messages,
              isLoadingRef.current && session.id === currentSessionIdRef.current,
              session.hasMoreHistory ?? false,
            ),
          })));

          // 分页接口已从 SQLite 原行投影思考链；打开页面无需再重建旧会话缓存。

          // 设置当前会话ID（校验有效性后再使用）
          const validCurrentId = response.current_session_id && frontendSessions.some(s => s.id === response.current_session_id)
            ? response.current_session_id
            : (frontendSessions.length > 0 ? frontendSessions[0].id : '');
          if (validCurrentId) {
            setCurrentSessionId(validCurrentId);
            // 同步修正后端的 current_session_id，防止指向已删除的会话
            if (validCurrentId !== response.current_session_id) {
              sessionApi.setCurrentSession(validCurrentId).catch(() => {});
            }
          }

          // 如果没有会话，先尝试从 localStorage 恢复
          if (frontendSessions.length === 0) {
            const lastId = localStorage.getItem('mirrow_last_session_id');
            let recovered = false;
            if (lastId) {
              const existing = await sessionApi.getSession(lastId, { limit: MESSAGE_WINDOW });
              if (existing) {
                const recoveredSession: Session = {
                  id: existing.id,
                  title: existing.title,
                  messages: reconcileServerMessages(
                    existing.id, [], existing.messages.map(apiMsg => mapApiMessageToUi(apiMsg)),
                  ),
                  createdAt: new Date(existing.createdAt),
                  updatedAt: new Date(existing.updatedAt),
                  locked: existing.locked || false,
                  isPrimary: existing.isPrimary || false,
                  hasMoreHistory: existing.hasMore ?? false,
                  tailStartId: existing.oldestId || undefined,
                  tailStartTs: existing.oldestTs || undefined,
                  tailStartRowId: existing.oldestRowId || undefined,
                };
                setSessions([recoveredSession]);
                setCurrentSessionId(existing.id);
                recovered = true;
              }
            }
            if (!recovered) {
              // 无会话且无本地恢复：保持空状态，用户手动创建
              setSessions([]);
              setCurrentSessionId('');
            }
          }
          // 成功加载，跳出重试循环
          setIsLoadingSessions(false);

          // 这里曾经在首屏之后「后台静默拉全量」补历史。主会话涨到两万多条后，
          // 那次请求变成 60MB，WebView 解析即卡死 —— 这正是每次启动都会卡一次、
          // 重启也救不回来的原因。历史改为用户点 ↑ 时按游标分页拉取
          // （见 loadMoreMessages）。

          return;
        } catch (error) {
          if (abortController.signal.aborted) {
            setIsLoadingSessions(false);
            return;
          }
          if (platform === 'mobile' && await refreshMobileBackendEndpoint()) {
            window.location.reload();
            return;
          }
          const isLastAttempt = attempt === MAX_RETRIES - 1;
          console.error(`加载会话失败 (尝试 ${attempt + 1}/${MAX_RETRIES}):`, error);

          if (isLastAttempt) {
            // 最终后备：本地恢复或纯本地会话
            const lastId = localStorage.getItem('mirrow_last_session_id');
            let recovered = false;
            if (lastId) {
              try {
                const existing = await sessionApi.getSession(lastId, { limit: MESSAGE_WINDOW });
                if (existing) {
                  const recoveredSession: Session = {
                    id: existing.id,
                    title: existing.title,
                    messages: reconcileServerMessages(
                      existing.id, [], existing.messages.map(apiMsg => mapApiMessageToUi(apiMsg)),
                    ),
                    createdAt: new Date(existing.createdAt),
                    updatedAt: new Date(existing.updatedAt),
                    locked: existing.locked || false,
                    isPrimary: existing.isPrimary || false,
                    hasMoreHistory: existing.hasMore ?? false,
                    tailStartId: existing.oldestId || undefined,
                    tailStartTs: existing.oldestTs || undefined,
                    tailStartRowId: existing.oldestRowId || undefined,
                  };
                  setSessions(prev => {
                    // 合并而非替换：保留已加载的其他会话。
                    const others = prev.filter(s => s.id !== existing.id);
                    return [...others, recoveredSession];
                  });
                  setCurrentSessionId(existing.id);
                  recovered = true;
                }
              } catch { /* localStorage 恢复失败，走新建逻辑 */ }
            }
            if (!recovered) {
              // 所有恢复均失败，保留门户会话，设置后台重试
              setSessions([]);
              setCurrentSessionId('');
              // 后台每30秒重试一次（Tailscale重连后自动恢复）
              const retryInterval = setInterval(async () => {
                try {
                  console.log('[loadSessions] 后台重试...');
                  if (platform === 'mobile' && await refreshMobileBackendEndpoint()) {
                    clearInterval(retryInterval);
                    window.location.reload();
                    return;
                  }
                  const resp = await sessionApi.getAllSessions(undefined, 200);
                  if (resp.sessions && resp.sessions.length > 0) {
                    console.log('[loadSessions] 后台重试成功！');
                    clearInterval(retryInterval);
                    // 重新触发完整加载
                    loadSessions();
                  }
                } catch { /* 继续等待 */ }
              }, 30000);
            }
            setIsLoadingSessions(false);
            return;
          }

          // 未到最大重试次数，等待后重试
          await delay(RETRY_DELAY_MS * (attempt + 1));
        }
      }
    }

    loadSessions();

    // 登录启动：异步调 /api/system/startup，结果通过 WebSocket 异步到达
    fetch(`${getApiBase()}/api/system/startup`, { method: 'POST' })
      .then(res => res.json())
      .then(data => {
        if (data.days_absent > 0) {
          console.log(`[STARTUP] 缺席 ${data.days_absent} 天，日历扫描已在后台执行`);
        }
      })
      .catch(e => console.error('启动通知失败:', e));

    return () => abortController.abort();
  }, [sessionsReload]);

  reloadSessionRef.current = reloadCurrentSession;

  // ── 未回复消息恢复轮询 ──
  const startRecoveryPolling = async () => {
    const sid = currentSessionIdRef.current;
    if (!sid) { console.log('[RECOVERY] No session id, skip'); return; }
    if (recoveryInProgressRef.current) { console.log('[RECOVERY] Already in progress, skip'); return; }
    if (isLoadingRef.current) { console.log('[RECOVERY] Already loading (normal chat), skip'); return; }

    // 先从服务端获取最新会话，确认是否真的需要恢复
    let session: any;
    try {
      // 只看最后一条消息，取最近一小段即可
      session = await sessionApi.getSession(sid, { limit: 20 });
    } catch {
      console.log('[RECOVERY] Backend unreachable, skip');
      return;
    }
    if (!session?.messages?.length) { console.log('[RECOVERY] No messages in session, skip'); return; }

    const lastMsg = session.messages[session.messages.length - 1];
    if (lastMsg.role !== 'user') { console.log('[RECOVERY] Last message is assistant/divider, no recovery needed'); return; }
    // 超过 5 分钟的旧消息不恢复（避免打开旧会话时误触发）
    const msgAge = Date.now() - new Date(lastMsg.timestamp).getTime();
    if (msgAge > 5 * 60 * 1000) { console.log(`[RECOVERY] Message too old (${(msgAge/60000).toFixed(1)}min), skip`); return; }

    // 确实需要恢复
    console.log('[RECOVERY] Unanswered message detected, starting polling...');
    recoveryInProgressRef.current = true;
    setIsLoading(true);

    const maxAttempts = 15;  // 30 秒
    for (let i = 0; i < maxAttempts; i++) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const updated = await sessionApi.getSession(sid, { limit: 20 });
        if (!updated?.messages?.length) continue;
        const updatedLast = updated.messages[updated.messages.length - 1];
        if (updatedLast.role === 'assistant' || updatedLast.role === 'divider') {
          // 回复已到达，更新会话
          console.log('[RECOVERY] Reply found on server, updating session');
          setSessions(prev => prev.map(s => {
            if (s.id !== sid) return s;
            const backendMsgs = updated.messages.map((apiMsg: any) => mapApiMessageToUi(apiMsg));
            return {
              ...s,
              messages: reconcileServerMessages(sid, s.messages || [], backendMsgs, false,
                updated.hasMore ?? false),
              hasMoreHistory: updated.hasMore ?? s.hasMoreHistory ?? false,
              updatedAt: new Date(),
            };
          }));
          setIsLoading(false);
          recoveryInProgressRef.current = false;
          return;
        }
      } catch {
        // 网络抖动，继续轮询
      }
    }

    // 超时：静默放弃，不追加错误消息（用户看到自己的消息还在，自然会重发）
    console.log('[RECOVERY] Polling timeout, giving up silently');
    setIsLoading(false);
    recoveryInProgressRef.current = false;
  };

  const handleMusicUpdate = (data: any) => {
    if (data.event === 'playback_interrupted') {
      const newMsg: Message = {
        id: `music_${data.timestamp || Date.now()}`,
        role: 'assistant',
        content: data.message || '',
        timestamp: new Date(),
        isMusic: true,
      };

      setSessions(prev => {
        const updated = prev.map(s => ({ ...s }));
        const activeIdx = updated.findIndex(s => s.id === currentSessionIdRef.current);
        if (activeIdx >= 0) {
          const session = { ...updated[activeIdx], messages: [...updated[activeIdx].messages] };
          // 避免重复添加
          const exists = session.messages.some(m => m.id === newMsg.id);
          if (!exists) {
            session.messages.push(newMsg);
            updated[activeIdx] = session;
            // The backend already persists this event under the same ID.
          }
        }
        return updated;
      });
    }
  };

  // 请求通知权限
  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }, []);

  // 切换会话时请求回到底部；等待 Virtuoso 完成布局后再落实滚动。
  const prevSessionRef = useRef(currentSessionId);
  useEffect(() => {
    if (currentSessionId && currentSessionId !== prevSessionRef.current) {
      prevSessionRef.current = currentSessionId;
      scrollToBottom();
    }
  }, [currentSessionId]);

  // 从其他页面或聊天子页面返回主聊天时，等待 Virtuoso 完成布局。
  useEffect(() => {
    if (view === 'chat' && chatView === 'normal') {
      scrollToBottom();
    }
  }, [view, chatView]);

  // 流式输出中：新消息或内容追加后滚到底部（加载期间由 rAF 接管，此处跳过）
  useEffect(() => {
    if (streamingTick > 0 && timelineMessages.length > 0 && !isLoading) {
      virtuosoRef.current?.scrollToIndex({
        index: timelineMessages.length - 1,
        align: 'end',
        behavior: 'auto'
      });
    }
  }, [streamingTick]);

  // Virtuoso 单独负责流式输出的底部锚定。不要再逐帧写 scrollTop，
  // 否则 Footer（正在输入/工具状态）变高时两套滚动控制会互相抢位置。
  useEffect(() => {
    isLoadingRef.current = isLoading;
  }, [isLoading]);

  // 思考过程展开/收起：最后一条消息用 rAF 持续追踪底部，匹配 CSS transition 逐帧变化
  useEffect(() => {
    const targetId = expandedThinkingId || prevExpandedIdRef.current;
    if (!targetId) return;
    const idx = timelineMessages.findIndex(m => m.id === targetId);
    if (idx < 0 || idx !== timelineMessages.length - 1) {
      prevExpandedIdRef.current = expandedThinkingId;
      return;
    }
    prevExpandedIdRef.current = expandedThinkingId;
    const scroller = messagesRef.current?.querySelector('[data-virtuoso-scroller]');
    if (!scroller) return;
    let raf = 0;
    const follow = () => {
      scroller.scrollTop = scroller.scrollHeight;
      raf = requestAnimationFrame(follow);
    };
    raf = requestAnimationFrame(follow);
    setTimeout(() => cancelAnimationFrame(raf), 350);
  }, [expandedThinkingId]);

  // Flush 缓存的 wander 消息：当 sessions 从空变为非空时注入
  useEffect(() => {
    if (pendingWanderMessages.length === 0) return;
    if (sessions.length === 0) return;
    const targetId = currentSessionIdRef.current || sessions[0]?.id;
    if (!targetId) return;
    setSessions(prev => {
      const updated = prev.map(s => {
        if (s.id !== targetId) return s;
        const existingIds = new Set(s.messages.map(m => m.id));
        const newMsgs = pendingWanderMessages.filter(m => !existingIds.has(m.id));
        if (newMsgs.length === 0) return s;
        return { ...s, messages: [...s.messages, ...newMsgs], updatedAt: new Date() };
      });
      return updated;
    });
    setPendingWanderMessages([]);
  }, [sessions, pendingWanderMessages]);

  // Wander 消息触发会话切换（避免在 setSessions updater 中产生副作用）
  useEffect(() => {
    if (pendingSessionSwitch) {
      setCurrentSessionId(pendingSessionSwitch);
      setPendingSessionSwitch(null);
    }
  }, [pendingSessionSwitch]);

  useEffect(() => {
    localStorage.setItem('aiPersona', JSON.stringify(persona));
    // 同步到后端设置（仅在后端数据已加载后才写入，防止默认值覆盖磁盘）
    if (!personaLoadedRef.current) return;
    const sequence = ++personaSaveSequenceRef.current;
    setPersonaSyncStatus('saving');
    const timer = window.setTimeout(() => {
      personaSaveChainRef.current = personaSaveChainRef.current
        .catch(() => undefined)
        .then(async () => {
          const response = await fetch(`${getApiBase()}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'aiPersona', value: persona })
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          if (sequence === personaSaveSequenceRef.current) {
            if (localStorage.getItem('mirrow_pending_ai_avatar') === persona.avatar) {
              localStorage.removeItem('mirrow_pending_ai_avatar');
            }
            setPersonaSyncStatus('saved');
          }
        })
        .catch(error => {
          console.error('人设与聊天背景同步失败:', error);
          if (sequence === personaSaveSequenceRef.current) setPersonaSyncStatus('error');
        });
    }, 280);
    return () => window.clearTimeout(timer);
  }, [persona]);

  useEffect(() => {
    localStorage.setItem('userAvatar', userAvatar);
    // 同步到后端设置（仅在后端 userAvatar 已加载后才写入，防止 localStorage 的默认值覆盖磁盘）
    if (!userAvatarLoadedRef.current) return;
    fetch(`${getApiBase()}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: 'userAvatar', value: userAvatar })
    }).then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (localStorage.getItem('mirrow_pending_user_avatar') === userAvatar) {
        localStorage.removeItem('mirrow_pending_user_avatar');
      }
    }).catch((err) => {
      console.error('[userAvatar] 保存到后端失败:', err);
    });
  }, [userAvatar]);

  useEffect(() => {
    if (currentSessionId) {
      localStorage.setItem('mirrow_last_session_id', currentSessionId);
    }
  }, [currentSessionId]);

  const handleUserAvatarUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    try {
      const avatar = await prepareAvatarImage(file);
      localStorage.setItem('mirrow_pending_user_avatar', avatar);
      setUserAvatar(avatar);
    } catch (error) {
      alert(error instanceof Error ? error.message : '头像处理失败');
    }
  };

  const handleAiAvatarUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    try {
      const avatar = await prepareAvatarImage(file);
      localStorage.setItem('mirrow_pending_ai_avatar', avatar);
      setPersona(current => ({ ...current, avatar }));
    } catch (error) {
      alert(error instanceof Error ? error.message : '头像处理失败');
    }
  };

  // 确保当前会话ID有效
  useEffect(() => {
    async function setDefaultCurrentSession() {
      if (currentSessionId === '' && sessions.length > 0) {
        const firstSessionId = sessions[0].id;
        setCurrentSessionId(firstSessionId);

        // 更新后端的当前会话ID
        try {
          await sessionApi.setCurrentSession(firstSessionId);
        } catch (error) {
          console.error('设置默认当前会话失败:', error);
        }
      }
    }

    setDefaultCurrentSession();
  }, [currentSessionId, sessions]);

  // 注意：会话数据现在存储在后端，不再使用localStorage
  // 所有会话操作都通过sessionApi与后端同步

  // 获取话题状态
  const fetchTopicStatus = async (sessionId: string): Promise<TopicStatus | null> => {
    try {
      const response = await fetch(`${getApiBase()}/api/topic/status/${sessionId}`);
      if (!response.ok) {
        return null;
      }
      const data = await response.json();
      return data as TopicStatus;
    } catch (error) {
      console.error('获取话题状态失败:', error);
      return null;
    }
  };

  // 获取已总结消息ID
  const fetchSummarizedStatus = async (sessionId: string): Promise<SummarizedStatus | null> => {
    try {
      const response = await fetch(`${getApiBase()}/api/messages/summarized/${sessionId}`);
      if (!response.ok) {
        return null;
      }
      const data = await response.json();
      return data as SummarizedStatus;
    } catch (error) {
      console.error('获取已总结消息ID失败:', error);
      return null;
    }
  };

  // 获取所有会话的话题状态
  const fetchAllTopicStatuses = async () => {
    // 使用 ref 读取最新 sessions，避免定时器中的 stale closure
    const currentSessions = sessionsRef.current;
    // 并行获取所有会话的话题状态
    const results = await Promise.all(
      currentSessions.map(async (session) => {
        const status = await fetchTopicStatus(session.id);
        return status ? [session.id, status] as const : null;
      })
    );
    const statuses: Record<string, TopicStatus> = Object.fromEntries(
      results.filter((r): r is readonly [string, TopicStatus] => r !== null)
    );
    setTopicStatuses(statuses);
  };

  // 获取漫想模式状态
  const fetchWanderStatus = async (): Promise<WanderStatus | null> => {
    try {
      const response = await fetch(`${getApiBase()}/api/wander/status`);
      if (!response.ok) {
        return null;
      }
      const data = await response.json();
      return data as WanderStatus;
    } catch (error) {
      console.error('获取漫想状态失败:', error);
      return null;
    }
  };

  // 当会话数量变化（增删）时立即获取话题状态
  useEffect(() => {
    if (sessions.length > 0) {
      fetchAllTopicStatuses();
    }
  }, [sessions.length]);

  // 稳定的定时器（不依赖 sessions 对象引用），避免每次消息更新时重启
  useEffect(() => {
    if (sessions.length === 0) return;
    const interval = setInterval(() => {
      fetchAllTopicStatuses();
    }, 10000);
    return () => clearInterval(interval);
  }, [sessions.length > 0]);

  // 当切换会话时获取已总结消息ID
  useEffect(() => {
    if (currentSessionId) {
      fetchSummarizedStatus(currentSessionId).then(status => {
        if (status) {
          setSummarizedStatus(status);
        }
      });
    }
  }, [currentSessionId]);

  // 获取漫想模式状态并定时刷新
  useEffect(() => {
    const updateWanderStatus = async () => {
      const status = await fetchWanderStatus();
      if (status) {
        setWanderStatus(status);
        // 同步后端用户状态到前端（刷新页面或重连时恢复）
        if (status.user_status !== undefined && status.user_status !== null) {
          setUserStatus(status.user_status);
        }
        // 同步 StatusMeta（层级元数据）
        if ((status as any).status_meta) {
          setStatusMeta((status as any).status_meta);
        }
      }
    };

    updateWanderStatus();

    // 每5秒刷新漫想状态
    const interval = setInterval(updateWanderStatus, 5000);

    return () => clearInterval(interval);
  }, []);

  // MIRROW 页面可见性心跳（供后端判断用户是否在 MIRROW 页面）
  useEffect(() => {
    const sendHeartbeat = () => {
      const nightRead = nightControl.captureRead();
      const browseRead = phoneBrowseControl.captureRead();
      const visible = document.visibilityState === 'visible' && document.hasFocus();
      fetch(`${getApiBase()}/api/mirrow/heartbeat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visible }),
      }).then(r => {
        if (!r.ok) throw new Error("heartbeat unavailable");
        return r.json();
      }).then(data => {
        if (data.ok === false) return;
        nightControl.acceptSnapshot(data.night_mode, nightRead);
        phoneBrowseControl.acceptSnapshot(data.phone_browse_mode, browseRead);
      }).catch(() => {});
    };

    sendHeartbeat();
    const interval = setInterval(sendHeartbeat, 3000);

    // 挂载时镜像恢复：后端还有整理任务在跑就把进度条拉回来
    checkTaskStatusOnce();

    // 页面可见性或焦点变化时立即上报；从后台恢复时检测卡死的加载状态
    const onVisibilityChange = () => {
      sendHeartbeat();
      if (document.visibilityState === 'visible') {
        // 不在 visibility/focus 事件里擅自释放聊天占用态。请求、工具二轮和
        // 语音识别各自拥有生命周期；跨页面回来只做服务端补拉。
        void activeChatRecoveryRef.current?.check();
        if (!isLoadingRef.current) startRecoveryPolling();
        // 从后台恢复后重载会话（WS可能已断，数据可能已旧）
        // This effect is mounted before loadSessions resolves. The captured
        // currentSessionId can remain empty forever, so use the live ref when
        // recovering reminders and other messages missed in the background.
        if (currentSessionIdRef.current) {
          void reloadCurrentSession();
        }
        // 切回前台镜像恢复：后端还有整理任务就重新显示进度条
        checkTaskStatusOnce();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('focus', onVisibilityChange);
    window.addEventListener('blur', onVisibilityChange);

    return () => {
      clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onVisibilityChange);
      window.removeEventListener('blur', onVisibilityChange);
    };
  }, []);

  // ── 页面加载/刷新后检测未回复消息 ──
  useEffect(() => {
    // 仍在加载会话列表时跳过；已有活跃聊天时跳过
    if (isLoadingSessions) return;
    if (isLoading) return;
    // 延迟一小段时间，确保 sessions 状态已从 loadSessions 的 setState 完成渲染
    const timer = setTimeout(() => startRecoveryPolling(), 500);
    return () => clearTimeout(timer);
  }, [isLoadingSessions, isLoading]);

  // 漫想WebSocket持久连接（接收主动消息推送）
  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let disposed = false;
    let pingInterval: ReturnType<typeof setInterval> | null = null;
    let receivedKeys = new Set<string>();

    const connectWanderWs = () => {
      // 关闭旧连接（先解除回调再关闭，防止 close() 触发 onclose 产生多余的重连定时器）
      if (wanderWsRef.current) {
        wanderWsRef.current.onclose = null;
        wanderWsRef.current.onerror = null;
        wanderWsRef.current.close();
        wanderWsRef.current = null;
      }

      const ws = new WebSocket(`${getWsBase()}/ws/wander?platform=${getPlatform()}`);

      ws.onmessage = (event) => {
        // 心跳消息跳过 JSON 解析
        if (event.data === 'ping' || event.data === 'pong') return;
        try {
          const data = JSON.parse(event.data);

          if (data.type === 'notification' || data.role === 'notification') {
            // 通用 UI-only 通知：持久化消息可在 reload 时出现，WS 只负责低延迟呈现。
            const notificationId = data.message_id || data.msg_id || `notification_${Date.now()}`;
            const notificationKey = `notification|${notificationId}`;
            if (receivedKeys.has(notificationKey) || wanderAddedMsgIds.current.has(notificationId)) return;
            receivedKeys.add(notificationKey);
            const notificationMsg: Message = mapApiMessageToUi({
              ...data,
              id: notificationId,
              role: data.role === 'event' ? 'event' : 'notification',
              content: data.content || data.message || '',
              timestamp: new Date(data.timestamp || Date.now()),
              eventType: data.eventType || data.event_type || undefined,
              // Checkup receipts arrive over WS before the session reload. Keep
              // their structured per-sensor outcomes on the realtime row too.
              toolCalls: data.toolCalls ?? data.tool_calls
                ?? (Array.isArray(data.checkup?.items) ? data.checkup.items : undefined),
              notificationMetadata: data.notificationMetadata || data.metadata,
            });
            wanderAddedMsgIds.current.add(notificationId);
            setSessions(prev => {
              if (prev.some(s => s.messages.some(m => m.id === notificationId))) return prev;
              const requested = data.session_id && prev.some(s => s.id === data.session_id)
                ? data.session_id : currentSessionIdRef.current;
              const targetId = requested && prev.some(s => s.id === requested)
                ? requested : (prev[0]?.id || '');
              if (targetId) {
                return prev.map(s => s.id === targetId
                  ? { ...s, messages: [...s.messages, notificationMsg], updatedAt: new Date() }
                  : s);
              }
              setPendingWanderMessages(prevPending => [...prevPending, notificationMsg]);
              return prev;
            });
            if (!currentSessionIdRef.current && data.session_id) {
              setPendingSessionSwitch(data.session_id);
            }
          } else if (data.type === 'quick_action_message' && ['ui_action:visit_lounge', 'ui_action:play_game', 'ui_action:visit_workshop'].includes(data.eventType)) {
            const invitation: Message = {id:data.message_id, role:'user', content:data.message,
              timestamp:new Date(data.timestamp), eventType:data.eventType};
            setSessions(prev => prev.map(s => s.id !== data.session_id || s.messages.some(m => m.id === invitation.id)
              ? s : {...s, messages:[...s.messages, invitation], updatedAt:new Date()}));
          } else if (data.type === 'wander_push') {
            // 去重：优先使用后端事件ID，回退到消息内容+时间戳
            const dedupKey = data.eventId || `${data.message?.slice(0, 80)}|${data.timestamp}`;
            if (receivedKeys.has(dedupKey)) {
              console.log('[WANDER_WS] 跳过重复消息:', data.message?.slice(0, 30));
              return;
            }
            receivedKeys.add(dedupKey);
            if (receivedKeys.size > 200) {
              const entries = [...receivedKeys];
              receivedKeys = new Set(entries.slice(-100));
            }

            console.log('[WANDER_WS] 收到漫想推送:', data.message?.slice(0, 50));
            // 漫想消息以AI回复格式呈现，并同步到后端存储/上下文/摘要/日记
            // 使用后端 eventId 作为消息ID，确保去重和撤回时前后端ID一致
            const wanderMsgId = data.eventId ? `wander_${data.eventId}` : `wander_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
            const newMsg: Message = mapApiMessageToUi({
              ...data,
              id: wanderMsgId,
              role: data.role === 'event' ? 'event' : 'assistant',
              content: data.message,
              timestamp: new Date(data.timestamp || Date.now()),
              isWander: data.isWander !== false,
              eventType: data.eventType || undefined,
              toolCalls: data.toolCalls || undefined,
              images: data.images || undefined,
              thinkingChain: data.thinkingChain || (data.reasoning ? [{ content: data.reasoning, timestamp: data.timestamp || new Date().toISOString() }] : undefined)
            });

            // Ref 级别去重：使用 eventId 作为 key，确保相同事件不重复添加
            if (wanderAddedMsgIds.current.has(wanderMsgId)) return;
            wanderAddedMsgIds.current.add(wanderMsgId);
            if (wanderAddedMsgIds.current.size > 200) {
              const entries = [...wanderAddedMsgIds.current];
              wanderAddedMsgIds.current = new Set(entries.slice(-100));
            }

            setSessions(prev => {
              // 防止 React Strict Mode 双重调用导致重复添加（状态更新器幂等性保护）
              if (prev.some(s => s.messages.some(m => m.id === newMsg.id))) {
                console.log('[WANDER_WS] 消息已存在，跳过重复添加:', newMsg.id);
                return prev;
              }

              const currentId = data.session_id || currentSessionIdRef.current;
              if (currentId && prev.some(s => s.id === currentId)) {
                console.log('[WANDER_WS] 添加到当前会话:', currentId);
                return prev.map(s =>
                  s.id === currentId
                    ? { ...s, messages: [...s.messages, newMsg], updatedAt: new Date() }
                    : s
                );
              }

              if (prev.length > 0) {
                const targetId = prev[0].id;
                console.log('[WANDER_WS] 无活跃会话，添加到首个会话:', targetId);
                return prev.map(s =>
                  s.id === targetId
                    ? { ...s, messages: [...s.messages, newMsg], updatedAt: new Date() }
                    : s
                );
              }

              // sessions 尚未加载完成，缓存 wander 消息，待加载完成后注入
              console.log('[WANDER_WS] 会话列表为空（可能仍在加载），缓存消息');
              setPendingWanderMessages(prev => [...prev, newMsg]);
              return prev;
            });

            // 无活跃会话但 sessions 存在 → 切换到首个会话（在 setSessions 外部处理，避免 updater 副作用）
            if (!currentSessionIdRef.current && sessions.length > 0) {
              setPendingSessionSwitch(sessions[0].id);
            }
          } else if (data.type === 'reminder_push') {
            // 首条提醒/查岗消息直接追加；后端同时已落盘，chat_reload 只作为补偿路径。
            const reminderMsgId = data.message_id || `reminder_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
            const dedupKey = `reminder|${reminderMsgId}`;
            if (receivedKeys.has(dedupKey)) return;
            receivedKeys.add(dedupKey);
            const reminderMsg: Message = mapApiMessageToUi({
              ...data,
              id: reminderMsgId,
              role: 'assistant',
              content: data.message || '',
              timestamp: new Date(data.timestamp || Date.now()),
              isReminder: data.isReminder !== false,
              eventType: data.eventType || undefined,
              toolCalls: data.toolCalls ?? data.tool_calls,
              thinkingChain: data.thinkingChain || (data.thinking ? [{
                content: data.thinking,
                timestamp: data.timestamp || new Date().toISOString(),
              }] : undefined),
            });
            if (wanderAddedMsgIds.current.has(reminderMsgId)) return;
            wanderAddedMsgIds.current.add(reminderMsgId);
            setSessions(prev => {
              if (prev.some(s => s.messages.some(m => m.id === reminderMsgId))) {
                if (!reminderMsg.thinkingChain?.length) return prev;
                return prev.map(s => ({ ...s, messages: s.messages.map(m =>
                  m.id === reminderMsgId && !m.thinkingChain?.length
                    ? { ...m, thinkingChain: reminderMsg.thinkingChain } : m
                ) }));
              }
              const requested = data.session_id && prev.some(s => s.id === data.session_id)
                ? data.session_id : currentSessionIdRef.current;
              const targetId = requested && prev.some(s => s.id === requested)
                ? requested : (prev[0]?.id || '');
              if (targetId) {
                return prev.map(s => s.id === targetId
                  ? { ...s, messages: [...s.messages, reminderMsg], updatedAt: new Date() }
                  : s);
              }
              setPendingWanderMessages(prevPending => [...prevPending, reminderMsg]);
              return prev;
            });
            if (!currentSessionIdRef.current && data.session_id) {
              setPendingSessionSwitch(data.session_id);
            }
          } else if (data.type === 'sentinel_push') {
            const dedupKey = data.eventId || `sentinel|${data.message?.slice(0, 80)}|${data.timestamp}`;
            if (receivedKeys.has(dedupKey)) return;
            receivedKeys.add(dedupKey);
            if (receivedKeys.size > 200) {
              const entries = [...receivedKeys];
              receivedKeys = new Set(entries.slice(-100));
            }
            console.log('[SENTINEL_WS] 收到哨兵推送:', data.message?.slice(0, 50));
            const sentinelMsgId = data.eventId || `sentinel_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
            const sentinelMsg: Message = mapApiMessageToUi({
              ...data,
              id: sentinelMsgId,
              role: 'assistant',
              content: data.message,
              timestamp: new Date(data.timestamp || Date.now()),
              isSentinel: true,
              toolCalls: data.toolCalls ?? data.tool_calls,
              thinkingChain: data.thinkingChain || (data.reasoning ? [{ content: data.reasoning, timestamp: data.timestamp || new Date().toISOString() }] : undefined),
            });
            if (wanderAddedMsgIds.current.has(sentinelMsgId)) return;
            wanderAddedMsgIds.current.add(sentinelMsgId);
            setSessions(prev => {
              if (prev.some(s => s.messages.some(m => m.id === sentinelMsg.id))) return prev;
              const currentId = currentSessionIdRef.current;
              if (currentId && prev.some(s => s.id === currentId)) {
                return prev.map(s =>
                  s.id === currentId
                    ? { ...s, messages: [...s.messages, sentinelMsg], updatedAt: new Date() }
                    : s
                );
              }

              if (prev.length > 0) {
                const targetId = prev[0].id;
                console.log('[SENTINEL_WS] 无活跃会话，添加到首个会话:', targetId);
                return prev.map(s =>
                  s.id === targetId
                    ? { ...s, messages: [...s.messages, sentinelMsg], updatedAt: new Date() }
                    : s
                );
              }

              console.log('[SENTINEL_WS] 会话列表为空（可能仍在加载），缓存消息');
              setPendingWanderMessages(prev => [...prev, sentinelMsg]);
              return prev;
            });

            if (!currentSessionIdRef.current && sessions.length > 0) {
              setPendingSessionSwitch(sessions[0].id);
            }
          } else if (data.type === 'sleep_prompt') {
            console.log('[SLEEP_PROMPT] 收到睡眠弹窗推送');
            setShowSleepPrompt(true);
          } else if (data.type === 'wander_status') {
            setWanderStatus(data.status);
            if (data.status?.user_status) {
              setUserStatus(data.status.user_status);
            }
            // 同步 StatusMeta
            if (data.status?.status_meta) {
              setStatusMeta(data.status.status_meta);
            }
          } else if (data.type === 'user_status_change') {
            // StateEngine 推送的二维状态变更（bridge.py WS push）
            if (data.legacy) setUserStatus(data.legacy);
            if (data.presence || data.activity || data.rest_mode) {
              setStatusMeta({
                legacy: data.legacy || userStatus,
                location: data.presence === 'out' ? 'out'
                  : (data.presence === 'at_computer' || data.presence === 'on_phone' || data.presence === 'away') ? 'home'
                  : 'unknown',
                activity_category: data.rest_mode && data.rest_mode !== 'none' ? data.rest_mode
                  : data.activity && data.activity !== 'none' ? data.activity
                  : data.presence === 'out' ? 'idle' : 'idle',
                rest_mode: data.rest_mode || 'none',
                source: data.source || 'auto',
              });
            }
          } else if (data.type === 'chat_reload') {
            reloadCurrentSession();
          } else if (data.type === 'phone_browse_push') {
            // 刷手机陪聊消息 → 直接追加到当前会话（参照 wander_push）
            const dedupKey = data.eventId || `pb|${data.message?.slice(0, 80)}|${data.timestamp}`;
            if (receivedKeys.has(dedupKey)) {
              console.log('[PHONE_BROWSE_WS] 跳过重复消息:', data.message?.slice(0, 30));
              return;
            }
            receivedKeys.add(dedupKey);
            if (receivedKeys.size > 200) {
              const entries = [...receivedKeys];
              receivedKeys = new Set(entries.slice(-100));
            }

            console.log('[PHONE_BROWSE_WS] 收到陪刷推送:', data.message?.slice(0, 50));
            const pbMsgId = data.eventId ? `phone_browse_${data.eventId}` : `phone_browse_${Date.now()}`;
            const pbMsg: Message = mapApiMessageToUi({
              ...data,
              id: pbMsgId,
              role: 'assistant',
              content: data.message,
              timestamp: new Date(data.timestamp || Date.now()),
              thinkingChain: data.thinkingChain || (data.thinking ? [{
                content: data.thinking,
                timestamp: data.timestamp || new Date().toISOString(),
              }] : undefined),
            });

            // Ref 级别去重
            if (wanderAddedMsgIds.current.has(pbMsgId)) return;
            wanderAddedMsgIds.current.add(pbMsgId);
            if (wanderAddedMsgIds.current.size > 200) {
              const entries = [...wanderAddedMsgIds.current];
              wanderAddedMsgIds.current = new Set(entries.slice(-100));
            }

            setSessions(prev => {
              if (prev.some(s => s.messages.some(m => m.id === pbMsg.id))) return prev;
              const currentId = currentSessionIdRef.current;
              if (currentId && prev.some(s => s.id === currentId)) {
                return prev.map(s =>
                  s.id === currentId
                    ? { ...s, messages: [...s.messages, pbMsg], updatedAt: new Date() }
                    : s
                );
              }
              if (prev.length > 0) {
                return prev.map(s =>
                  s.id === prev[0].id
                    ? { ...s, messages: [...s.messages, pbMsg], updatedAt: new Date() }
                    : s
                );
              }
              return prev;
            });
          } else if (data.type === 'music_cochlea_push') {
            // 音乐耳蜗推送 → 直接追加到当前会话
            const mcMsgId = data.msg_id || `music_cochlea_${Date.now()}`;
            if (wanderAddedMsgIds.current.has(mcMsgId)) return;
            wanderAddedMsgIds.current.add(mcMsgId);

            const mcMsg: Message = mapApiMessageToUi({
              ...data,
              id: mcMsgId,
              role: 'assistant',
              content: data.message,
              isMusicCochlea: true,
              timestamp: new Date(data.timestamp || Date.now()),
              toolCalls: data.toolCalls ?? data.tool_calls ?? [],
              thinkingChain: data.thinkingChain || (data.thinking ? [{
                content: data.thinking,
                timestamp: data.timestamp || new Date().toISOString(),
              }] : undefined),
            });

            setSessions(prev => {
              if (prev.some(s => s.messages.some(m => m.id === mcMsg.id))) return prev;
              const currentId = currentSessionIdRef.current;
              if (currentId && prev.some(s => s.id === currentId)) {
                return prev.map(s =>
                  s.id === currentId
                    ? { ...s, messages: [...s.messages, mcMsg], updatedAt: new Date() }
                    : s
                );
              }
              return prev;
            });
          } else if (data.type === 'mobile_tool_request') {
            void relayMobileToolRequest(data, ws, (window as any).Capacitor?.Plugins?.MobileTool);
          } else {}
        } catch (e) {
          console.error('漫想WS消息解析失败:', e);
        }
      };

      ws.onclose = () => {
        if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
        // 如果组件已卸载（React Strict Mode 清理），不再重连
        if (disposed) {
          console.log('[WANDER_WS] 组件已卸载，不重连');
          return;
        }
        // 3秒后自动重连
        reconnectTimer = setTimeout(connectWanderWs, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };

      // 每15秒发送 ping 保持连接活跃，防止后端超时断开
      if (pingInterval) clearInterval(pingInterval);
      pingInterval = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send('ping');
        }
      }, 15000);

      wanderWsRef.current = ws;
    };

    connectWanderWs();

    return () => {
      disposed = true;
      clearTimeout(reconnectTimer);
      if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
      if (wanderWsRef.current) {
        wanderWsRef.current.onclose = null;
        wanderWsRef.current.onerror = null;
        if (wanderWsRef.current.readyState === WebSocket.OPEN) {
          wanderWsRef.current.close();
        }
        wanderWsRef.current = null;
      }
    };
  }, []);


  // ── 网易云音乐 WebSocket ──────────────────────────────────────

  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let disposed = false;

    const connectMusicWs = () => {
      if (musicWsRef.current) {
        musicWsRef.current.onclose = null;
        musicWsRef.current.onerror = null;
        musicWsRef.current.close();
        musicWsRef.current = null;
      }

      const ws = new WebSocket(`${getWsBase()}/ws/music?platform=${getPlatform()}`);

      ws.onmessage = (event) => {
        if (event.data === 'pong') return;
        if (event.data === 'ping') {
          // 服务端 30s 保活探测 → 回 pong（顺带刷新服务端接收超时）
          if (ws.readyState === WebSocket.OPEN) ws.send('pong');
          return;
        }
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'music_update') {
            console.log('[MUSIC_WS] 收到音乐更新:', data.event, data.message);
            handleMusicUpdate(data);
          }
        } catch (e) {
          console.error('音乐WS消息解析失败:', e);
        }
      };

      ws.onclose = () => {
        if (disposed) return;
        reconnectTimer = setTimeout(connectMusicWs, 5000);
      };

      ws.onerror = () => {
        ws.close();
      };

      musicWsRef.current = ws;
    };

    connectMusicWs();

    return () => {
      disposed = true;
      clearTimeout(reconnectTimer);
      if (musicWsRef.current) {
        musicWsRef.current.onclose = null;
        musicWsRef.current.onerror = null;
        if (musicWsRef.current.readyState === WebSocket.OPEN) {
          musicWsRef.current.close();
        }
        musicWsRef.current = null;
      }
    };
  }, []);



  // 获取记忆库数据

  // 格式化时间戳：当年不显示年份、当月不显示月份、当天不显示日期
  const formatTimestamp = useCallback((date: Date): string => {
    const now = new Date();
    const isSameYear = date.getFullYear() === now.getFullYear();
    const isSameMonth = isSameYear && date.getMonth() === now.getMonth();
    const isSameDay = isSameMonth && date.getDate() === now.getDate();

    if (isSameDay) {
      return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    } else if (isSameMonth) {
      return date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } else if (isSameYear) {
      return date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } else {
      return date.toLocaleString('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }
  }, []);

  // 导出消息记录为 Markdown
  const exportMessagesToMarkdown = () => {
    const session = getCurrentSession();
    if (!session) return;

    let markdown = `# ${session.title}\n\n`;
    markdown += `> 导出时间: ${new Date().toLocaleString()}\n`;
    markdown += `> 会话ID: ${session.id}\n\n`;
    markdown += `---\n\n`;

    for (const message of session.messages) {
      if (message.role === 'divider') {
        markdown += `\n---\n**${message.content}**\n---\n\n`;
        continue;
      }

      const roleLabel = message.role === 'user' ? '👤 用户' : message.isSentinel ? '🛡️ AI的哨兵' : message.isWander ? '💭 AI的漫想' : '🤖 AI助手';
      const time = formatTimestamp(message.timestamp);
      markdown += `### ${roleLabel} (${time})\n\n`;
      markdown += `${message.content}\n\n`;

      // 添加思维链
      if (message.thinkingChain && message.thinkingChain.length > 0) {
        markdown += `<details>\n<summary>💭 思考过程 (${message.thinkingChain.length}步)</summary>\n\n`;
        for (let i = 0; i < message.thinkingChain.length; i++) {
          const step = message.thinkingChain[i];
          markdown += `#### 步骤 ${i + 1}\n\n${step.content}\n\n`;
        }
        markdown += `</details>\n\n`;
      }

      markdown += `---\n\n`;
    }

    // 创建下载
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${session.title.replace(/[\\/:*?"<>|]/g, '_')}_${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // 导入处理函数 — 使用动态 API 基础地址以支持移动端 Tailscale 访问
  const API_BASE = getApiBase();


  // 开机自启开关
  const toggleAutoStart = async (component: 'mirrow', enable: boolean) => {
    const labels = { mirrow: 'MIRROW' };
    try {
      const res = await fetch(`${getApiBase()}/api/auto-start/${component}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enable }),
      });
      const data = await res.json();
      if (data.ok) {
        setAutoStart(prev => ({ ...prev, [component]: enable }));
        showToast('success', `${labels[component]} 开机自启已${enable ? '开启' : '关闭'}`);
      } else {
        showToast('error', `${labels[component]} 设置失败：${data.error || '未知错误'}`);
      }
    } catch (err) {
      showToast('error', `${labels[component]} 设置失败：${String(err)}`);
    }
  };


  

  

  const handleSend = (messageText: string, imageFiles: File[], voiceData?: { voiceAttachment: VoiceAttachment; voiceProsody: Record<string, any> }, longTextCard?: import('./types').LongTextCard, sessionOverride?: Session, preparedUserMessage?: Message): Promise<void> | false => {
    if (mainSendInFlightRef.current || isLoadingRef.current) return false;
    if (resendInFlightRef.current.size && !preparedUserMessage) return false;
    if (!messageText && imageFiles.length === 0 && !voiceData && !longTextCard) return false;

    const currentSession = sessionOverride || getCurrentSession();
    if (!currentSession) return false;
    const baseUserMessage: Message = preparedUserMessage || {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: 'user',
      content: messageText,
      timestamp: new Date(),
      voiceAttachment: voiceData?.voiceAttachment,
      longTextCard,
    };
    let draft: ReturnType<typeof createOutgoingDraft>;
    try { draft = createOutgoingDraft(baseUserMessage, imageFiles); }
    catch (cause) { return Promise.reject(new OutgoingPreparationError(cause)); }
    // Claim before image conversion or any other await. React loading state is
    // not a synchronous double-tap fence on mobile.
    mainSendInFlightRef.current = true;
    isLoadingRef.current = true;
    const feedback = chatTurnFeedback.begin();
    let preparationToken: string | null = null;
    const preparation = prepareOutgoingMessage(draft, {
      publish: message => upsertSessionMessage(currentSession.id, message),
      discard: id => { if (preparedUserMessage) return; setSessions(previous => previous.map(session => session.id === currentSession.id
        ? {...session, messages: session.messages.filter(message => message.id !== id)} : session)); },
      preparing: active => {
        if (active) preparationToken = beginInputActivity('send_preparation', '正在准备发送…');
        else if (preparationToken) { endInputActivity(preparationToken); preparationToken = null; }
      },
    });
    setIsLoading(true);
    setToolCallStatus([]);

    const runSend = async () => {
    const userMessage = await preparation;
    const imageAttachments = userMessage.images || [];

    // 保存完整的图片数据用于发送（发送后会被清除）
    // Accepted resends resolve original attachments on the server by message ID.
    const imagesToSend = preparedUserMessage ? [] : [...imageAttachments];

    // 检查是否是divider后的第一条消息
    const lastMessage = currentSession.messages[currentSession.messages.length - 1];
    const isAfterDivider = lastMessage?.role === 'divider';

    // The same local ID is already visible; preparation replaced only that row.
    const updatedMessages = upsertOutgoingMessage(
      sessionsRef.current.find(session => session.id === currentSession.id)?.messages || currentSession.messages,
      userMessage,
    );
    covenant.tryActivate(messageText, currentSession.id);

    // 预生成助手消息ID，发送给后端用于V2存储，保证ID与前端一致
    const pendingAssistantId = (Date.now() + 2000).toString();
    
    let refreshStarted = false;
    const refreshTurnMessages = () => {
      if (refreshStarted) return;
      refreshStarted = true;
      const snapshot = captureDisplayRows((sessionsRef.current.find(session => session.id === currentSession.id)?.messages || []).slice(-MESSAGE_WINDOW), messageMutationInFlightRef.current);
      void feedback.refresh(async isCurrent => {
        const page = await sessionApi.getSession(currentSession.id, { limit: MESSAGE_WINDOW });
        if (!page) throw new Error('会话暂不可用');
        const messages = page.messages.map(mapApiMessageToUi);
        setSessions(previous => previous.map(session => session.id === currentSession.id ? {
          ...session,
          messages: reconcileServerMessages(currentSession.id, session.messages || [],
            protectNewerDisplayRows(session.messages || [], messages, snapshot, messageMutationInFlightRef.current), true, page.hasMore ?? false),
          hasMoreHistory: isCurrent() ? page.hasMore ?? session.hasMoreHistory ?? false : session.hasMoreHistory,
          updatedAt: isCurrent() ? new Date(page.updatedAt) : session.updatedAt,
        } : session));
      }, () => showToast('error', '回复已结束，记录刷新暂未成功；已有消息和草稿已保留，可稍后刷新。'));
    };

    try {
      let waitForTurnCompletion: (() => Promise<void>) | undefined;
      const callFallbackChat = async () => {
        // 准备图片数据（与WebSocket同样的base64格式）- 使用保存的完整图片数据
        const fallbackImageData = imagesToSend.length > 0 ? imagesToSend.map(img => ({
          filename: img.filename,
          type: img.type,
          data: img.url
        })) : undefined;
        const fallbackResponse = await fetch(`${getApiBase()}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: userMessage.content,
            model: model,
            use_memory: useMemory,
            system_prompt: persona.systemPrompt || '',
            session_id: currentSessionId,
            is_after_divider: isAfterDivider,
            user_message_id: userMessage.id,
            accepted_resend: Boolean(preparedUserMessage),
            assistant_message_id: pendingAssistantId,
            images: fallbackImageData,
            image_model: imageModel,
            music_mode: musicMode,
            voice_prosody: voiceData?.voiceProsody,
            voice_attachment: voiceData?.voiceAttachment,
            long_text_card: longTextCard,
          })
        });

        if (!fallbackResponse.ok) {
          const fallbackText = await fallbackResponse.text();
          throw new Error(`Fallback chat failed: ${fallbackResponse.status} ${fallbackText || fallbackResponse.statusText}`);
        }

        const fallbackData = await fallbackResponse.json();
        // WS 已处理过此消息 → 从后端拉取 WS 已持久化的回复
        if (fallbackData.duplicate === true) {
          console.log('[FALLBACK] WS已处理此消息，从后端拉取最新数据');
          // Duplicate admission may mean the original tool round is still running.
          // Its real terminal receipt owns completion, not this HTTP response.
          await waitForTurnCompletion?.();
          return;
        }
        console.log('[FALLBACK] WS 已断开，等待后端持久化完成后全量刷新消息列表');

        // 更新话题状态（后端已在响应中返回）
        if (fallbackData.topic_status) {
          setTopicStatuses(prev => ({
            ...prev,
            [currentSessionId]: fallbackData.topic_status
          }));
        }

        // HTTP returns after durable completion; the bounded display refresh
        // is independent of the next send and cannot overwrite a newer turn.
        
        refreshTurnMessages();
      };

      const recoverAfterTransportFailure = async () => {
        feedback.phase('recovering');
        try {
          // A lost socket may follow a persisted failed turn. Resume its
          // receipt instead of generating (and executing tools) a second time.
          const status = await sessionApi.getChatTurnStatus(currentSessionId, userMessage.id);
          if (status !== 'unknown') {
            await waitForTurnCompletion?.();
            return;
          }
          await callFallbackChat();
        } catch (error) {
          console.error('Fallback chat failed:', error);
          showToast('error', '发送结果暂未确认，已保留聊天记录，请先刷新确认再重试');
          try { await reloadCurrentSession(); } catch { /* Keep the optimistic row. */ }
        }
      };

      // 使用WebSocket连接行为调度器（支持实时状态推送）
      await new Promise<void>((resolve) => {
        const ws = new WebSocket(`${getWsBase()}/ws/behavior?platform=${getPlatform()}`);

        let thinkingChain: sessionApi.ThinkingStep[] = [];
        let intermediateMessages: Message[] = []; // 累积所有中间LLM回复
        let lastIntermediateMessageId: string | null = null; // 追踪最后一条中间消息ID
        let committedMessages: Message[] = [...updatedMessages];
        let waitingForIntentCheck = false;
        let resolved = false;  // 防止 onclose 重复触发 fallback
        const finish = () => {
          resolved = true;
          recovery.dispose();
          ws.close();
          resolve();
        };
        const recovery: ChatTurnRecovery = createChatTurnRecovery({
          readStatus: () => sessionApi.getChatTurnStatus(currentSessionId, userMessage.id),
          isVisible: () => document.visibilityState === 'visible',
          onTerminal: status => {
            
            if (status === 'failed') showToast('error', '这一轮回复失败，已结束等待，可以重新发送；记录正在后台同步。');
            refreshTurnMessages();
            finish();
          },
        });
        activeChatRecoveryRef.current = recovery;
        waitForTurnCompletion = () => { void recovery.check(); return recovery.finished; };
        // 每轮工具调用完成时立即挂到当前中间消息上，不在最后堆积
        let pendingToolCallsForCurrentMessage: ToolCallRecord[] = [];
        let lastToolCallTargetId: string | null = null; // tool_executing 时记下目标消息ID

        ws.onopen = () => {
          if (resolved) return;
          // 准备图片数据（base64格式）- 使用保存的完整图片数据
          const imageData = imagesToSend.length > 0 ? imagesToSend.map(img => ({
            filename: img.filename,
            type: img.type,
            data: img.url // 已经是 base64 data URL
          })) : undefined;

          ws.send(JSON.stringify({
          message: userMessage.content,
            session_id: currentSessionId,
            system_prompt: persona.systemPrompt || '',
            images: imageData,
            image_model: imageModel,
            user_message_id: userMessage.id,
            accepted_resend: Boolean(preparedUserMessage),
            assistant_message_id: pendingAssistantId,
            music_mode: musicMode,
            voice_prosody: voiceData?.voiceProsody,
            voice_attachment: voiceData?.voiceAttachment,
            long_text_card: longTextCard,
          }));
          // The socket may remain open while its final frame is lost. Follow
          // this exact turn independently until its real terminal receipt.
          void recovery.check();
        };

        ws.onmessage = async (event) => {
          if (resolved) return;
          const data = JSON.parse(event.data);
          if (data.type === 'result' && ((data.session_id && data.session_id !== currentSessionId) ||
              (data.user_message_id && data.user_message_id !== userMessage.id))) return;

          if (data.error) {
            console.error('WebSocket错误:', data.error);
            resolved = true;
            ws.close();
            await recoverAfterTransportFailure();
            resolve();
            return;
          }

          // ── 手机工具中继 ──
          if (data.type === 'mobile_tool_request') {
            await relayMobileToolRequest(data, ws, (window as any).Capacitor?.Plugins?.MobileTool);
            return;
          }

          if (data.type === 'status') {
            const { status, data: statusData } = data;

            if (status === 'reply_finalizing') {
              if (statusData?.session_id === currentSession.id && statusData?.user_message_id === userMessage.id) feedback.phase('saving');
              return;
            }

            if (status === 'routing_switch') {
              showToast(`线路中断，正在自动切换（${statusData.attempt}/${statusData.total}）`);
              return;
            }
            if (status === 'routing_refusal') {
              showToast('模型明确拒绝了本次请求，详情见线路记录');
              return;
            }
            if (status === 'routing_failure') {
              showToast('当前线路均未能完成回复，请稍后重试；已完成的操作已保留。');
              return;
            }

            if (status === 'llm_reply') {
              feedback.phase('replying');
              // LLM的中间自然语言回复 - 创建新消息或更新最后一条
              const content = statusData.content;

              // 如果有内容，创建或更新中间消息
              if (content && content.trim()) {
                // 服务端带 message_id 时，若与上一条不同 → 强制创建新消息
                // 防止 mid-loop retry NL 覆盖 preview NL（无 tool_executing 重置时）
                const incomingId = statusData.message_id;
                if (incomingId && lastIntermediateMessageId && incomingId !== lastIntermediateMessageId) {
                  lastIntermediateMessageId = null;
                }
                // 检查是否需要创建新消息（首次或工具执行后）
                if (lastIntermediateMessageId === null) {
                  // 创建新的中间消息，使用服务端预生成的 message_id 与 session 持久化的 im_ ID 一致，前端按 ID 去重
                  const newMessageId = statusData.message_id || Date.now().toString();
                  lastIntermediateMessageId = newMessageId;
                  const roundThinking: sessionApi.ThinkingStep[] | undefined =
                    statusData.reasoning
                      ? [{ content: statusData.reasoning, timestamp: new Date().toISOString() }]
                      : undefined;
              const cleanContent = content.replace(/\n?TOOL_CALL:?\s*[\s\S]*$/, '').trim();
              const newMessage: Message = {
                    id: newMessageId,
                    role: 'assistant',
                    content: cleanContent,
                    timestamp: new Date(),
                    thinkingChain: roundThinking,
                    modelSource: statusData.modelSource,
                    toolCalls: pendingToolCallsForCurrentMessage.length > 0
                      ? [...pendingToolCallsForCurrentMessage]
                      : undefined
                  };
                  intermediateMessages.push(newMessage);
                  pendingToolCallsForCurrentMessage = [];
                  // 新消息 → 更新 state
                  const partialMessages = waitingForIntentCheck
                    ? [...committedMessages]
                    : [...updatedMessages, ...intermediateMessages];
                  if (waitingForIntentCheck) {
                    const committedIds = new Set(partialMessages.map(m => m.id));
                    for (const msg of intermediateMessages) {
                      if (!committedIds.has(msg.id)) partialMessages.push(msg);
                    }
                  }
                  await updateSessionMessages(currentSessionId, partialMessages, { preserveNotifications: true });
                  setStreamingTick(t => t + 1);
                } else {
                  const lastMsg = intermediateMessages.find(m => m.id === lastIntermediateMessageId);
                  if (lastMsg) {
                    lastMsg.content = content;
                    lastMsg.modelSource = statusData.modelSource;
                    setStreamingTick(t => t + 1);
                  }
                }
              }
            }
            else if (status === 'tool_executing') {
              feedback.phase('replying');
              // 工具开始执行 - 重置消息创建标记，下次 llm_reply 会创建新消息
              // 注意：不重置 pendingToolCallsForCurrentMessage，避免多工具路径中前面的结果丢失
              lastIntermediateMessageId = null;
              // 记下本轮工具调用应该挂到哪条消息（最后一条中间消息）
              lastToolCallTargetId = intermediateMessages.length > 0
                ? intermediateMessages[intermediateMessages.length - 1].id
                : null;

              const toolDesc = getToolDescription(statusData.tool, statusData.description);
              setToolCallStatus(prev => [...prev, {
                tool: statusData.tool,
                status: 'executing',
                description: toolDesc
              }]);
            }
            else if (status === 'tool_complete') {
              // 工具执行完成 — 立即挂载到当前中间消息
              const toolDesc = getToolDescription(statusData.tool, statusData.description);
              const toolRecord: ToolCallRecord = {
                tool: statusData.tool,
                description: toolDesc,
                result: statusData.result,
                success: statusData.success,
                ...(statusData.tool === 'band_touch' ? { parameters: {
                  action: statusData.parameters?.action,
                  note: statusData.parameters?.note,
                  pattern: statusData.parameters?.pattern,
                }} : {}),
                status: statusData.status || (statusData.success === false ? 'failure' : 'success'),
                error: statusData.error,
                extra_data: statusData.tool === 'band_touch'
                  ? { ...statusData.extra_data, ui_receipt_pending: true }
                  : statusData.extra_data
              };
              // 挂到当前的中间消息上
              pendingToolCallsForCurrentMessage.push(toolRecord);
              // 用 tool_executing 时记下的目标ID，因为 lastIntermediateMessageId 已被重置
              const targetId = lastToolCallTargetId || lastIntermediateMessageId;
              const currentMsg = intermediateMessages.find(m => m.id === targetId);
              if (currentMsg) {
                currentMsg.toolCalls = [...(currentMsg.toolCalls || []), ...pendingToolCallsForCurrentMessage];
                // 已挂载，清空待处理列表，防止下一轮消息继承同一批工具调用
                pendingToolCallsForCurrentMessage = [];
                // 实时更新UI让工具调用记录可见
                const partialMessages = waitingForIntentCheck
                  ? [...committedMessages]
                  : [...updatedMessages, ...intermediateMessages];
                if (waitingForIntentCheck) {
                  const committedIds = new Set(partialMessages.map(m => m.id));
                  for (const msg of intermediateMessages) {
                    if (!committedIds.has(msg.id)) partialMessages.push(msg);
                  }
                }
                await updateSessionMessages(currentSessionId, partialMessages, { preserveNotifications: true });
              }
              setToolCallStatus(prev => prev.map(tc =>
                tc.tool === statusData.tool && tc.status === 'executing'
                  ? { ...tc, status: 'complete' }
                  : tc
              ));
            }
            else if (status === 'thinking') {
              feedback.phase('replying');
              // LLM正在思考
              console.log('LLM思考中, round:', statusData.round);
            }
            else if (status === 'intent_check_done') {
              waitingForIntentCheck = false;
              committedMessages = [...committedMessages];

              const committedIds = new Set(committedMessages.map(m => m.id));
              for (const msg of intermediateMessages) {
                if (!committedIds.has(msg.id)) {
                  committedMessages.push(msg);
                }
              }

              if (statusData.has_tool_call && statusData.final_content) {
                const finalAsyncId = `intent_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
                const alreadyShown = committedMessages.some(m => m.role === 'assistant' && m.content === statusData.final_content);
                if (!alreadyShown) {
                  committedMessages.push({
                    id: finalAsyncId,
                    role: 'assistant',
                    content: statusData.final_content,
                    timestamp: new Date(),
                    // llm_reply 已将思考过程挂到对应中间消息，新消息仅在无中间消息时回退
                    thinkingChain: intermediateMessages.length === 0
                      ? statusData.thinking_chain
                      : undefined,
                    toolCalls: undefined
                  });
                }
              }

              await updateSessionMessages(currentSessionId, committedMessages, { preserveNotifications: true });
              if (resolved) return;
              finish();
            }
          }
          else if (data.type === 'result') {
            // 最终结果
            console.log('[DEBUG] WebSocket result thinking_chain:', data.thinking_chain);
            if (data.turn_status === 'failed' || data.failure_reason) {
              // A terminal failure releases admission; real tool receipts are
              // refreshed independently, without replaying generation.
              await recovery.check('failed');
              return;
            }

            

            // 构建最终消息列表：内容已通过 llm_reply 实时推送，result 仅携带元数据
            let finalMessages = [...updatedMessages, ...settleBandReceipts(intermediateMessages, data.tool_calls)];
            if (data.checkup_receipt_only === true) {
              const sensoryTools = new Set(['eyes', 'check_phone', 'band', 'capture_camera', 'take_screenshot']);
              finalMessages = finalMessages.map(message => {
                if (!message.toolCalls?.some(call => sensoryTools.has(call.tool))) return message;
                const remaining = message.toolCalls.filter(call => !sensoryTools.has(call.tool));
                return { ...message, toolCalls: remaining.length ? remaining : undefined, uiReceiptCarrier: true };
              });
            }

            // 补全最后一条中间消息的 thinkingChain（如有缺失）
            if (intermediateMessages.length > 0) {
              const lastIntermediate = intermediateMessages[intermediateMessages.length - 1];
              if (!lastIntermediate.thinkingChain || lastIntermediate.thinkingChain.length === 0) {
                lastIntermediate.thinkingChain = data.thinking_chain || thinkingChain;
                finalMessages = finalMessages.map(message => message.id === lastIntermediate.id
                  ? { ...message, thinkingChain: lastIntermediate.thinkingChain } : message);
              }
            }

            // 如果话题结束，添加divider
            if (data.topic_ended && data.divider_index >= 0) {
              const dividerMessage: Message = {
                id: (Date.now() + 1000).toString(),
                role: 'divider',
                content: '-刚刚聊的话题结束了-',
                timestamp: new Date(),
                thinkingChain: undefined
              };
              finalMessages.splice(data.divider_index + 1, 0, dividerMessage);
            }

            await updateSessionMessages(currentSessionId, finalMessages, { preserveNotifications: true });
            if (resolved) return;
            committedMessages = finalMessages;
            // A terminal provider failure must release the send owner even if
            // stale intent metadata remains. finish() also suppresses HTTP replay.
            waitingForIntentCheck = data.turn_status !== 'failed' && !data.failure_reason && data.pending_intent_check === true;

            // 更新话题状态
            if (data.topic_status) {
              setTopicStatuses(prev => ({
                ...prev,
                [currentSessionId]: data.topic_status
              }));
            } else {
              // fallback: 通过 API 刷新话题状态
              void fetchTopicStatus(currentSessionId).then(fallbackStatus => {
                if (!fallbackStatus) return;
                setTopicStatuses(prev => ({
                  ...prev,
                  [currentSessionId]: fallbackStatus
                }));
              }).catch(() => { /* Topic metadata cannot hold a completed chat lock. */ });
            }

            if (!waitingForIntentCheck) {
              finish();
            }
          }
        };

        ws.onerror = async (error) => {
          if (resolved) return;
          console.error('WebSocket错误:', error);
          resolved = true;
          ws.close();
          await recoverAfterTransportFailure();
          resolve();
        };

        ws.onclose = () => {
          if (!resolved) {
            console.warn('WebSocket closed without response, falling back to HTTP');
            resolved = true;
            void recoverAfterTransportFailure().finally(resolve);
          }
        };
      });
    } catch (error) {
      console.error('Error:', error);
      const errorMessage: Message = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: '抱歉，出现错误。请检查后端服务器是否运行。',
        timestamp: new Date(),
        thinkingChain: undefined
      };

      const errorMessages = [...updatedMessages, errorMessage];
      await updateSessionMessages(currentSessionId, errorMessages, { preserveNotifications: true });
    } finally {
      
      if (longTextCard?.kind === 'music') refreshTurnMessages();
    }
    };
    return runSend().finally(() => {
      activeChatRecoveryRef.current?.dispose();
      activeChatRecoveryRef.current = null;
      mainSendInFlightRef.current = false;
      isLoadingRef.current = false;
      setIsLoading(false);
      setToolCallStatus([]);
      feedback.complete();
    });
  };

  // 工具描述映射（优先使用后端提供的描述，前端作为 fallback）
  const getToolDescription = (toolName: string, backendDescription?: string): string => {
    // 朋友圈是一次性 UI-only 操作；实时状态也不能把 action 或后端
    // description 泄露到聊天工具挂载里。
    if (toolName === 'manage_social_feed') return '打开了共域';
    if (backendDescription && backendDescription.trim()) {
      let readableDescription = backendDescription.replace(/third_state_capability/gi, '浏览小红书').trim();
      if (readableDescription !== toolName) {
        const speaker = persona.name.trim();
        // 后端描述有时已经是“AI看人类伙伴”；先剥掉已有说话者，再统一补一次。
        if (speaker) {
          while (readableDescription.startsWith(speaker)) {
            readableDescription = readableDescription.slice(speaker.length).trimStart();
          }
        }
        return `${speaker}${readableDescription.replace(/^正在/, '')}`;
      }
    }
    const toolDescriptions: Record<string, string> = {
      'get_weather': `${persona.name}正在查询天气`,
      'take_screenshot': `${persona.name}正在截取屏幕画面`,
      'capture_camera': `${persona.name}正在通过摄像头观察`,
      'create_scheduled_task': `${persona.name}正在添加日程提醒`,
      'manage_scheduled_task': `${persona.name}正在管理日程提醒`,
      'shell': `${persona.name}正在执行系统命令`,
      'http_request': `${persona.name}正在发送网络请求`,
      'file': `${persona.name}正在操作文件`,
      'web_browser': `${persona.name}正在浏览网页`,
      'web_search': `${persona.name}正在搜索网络`,
      'third_state_capability': `${persona.name}正在浏览小红书`,
      'browse_xiaohongshu': `${persona.name}正在浏览小红书`,
      'documentation_check': `${persona.name}正在查阅文档`,
      'package_manager': `${persona.name}正在管理软件包`,
      'advanced_file': `${persona.name}正在操作文件`,
      'code_runner': `${persona.name}正在运行代码`,
      'toys': `${persona.name}正在控制蓝牙玩具`,
    };
    return toolDescriptions[toolName] || `${persona.name}正在使用工具`;
  };

  const handleStatusChange = (status: string, customText: string) => {
    setUserStatus(status);
    fetch(`${getApiBase()}/api/wander/user-status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, custom_text: customText })
    }).catch(e => console.error('设置用户状态失败:', e));
  };

  // 任务进度轮询：抽出复用（handleTopicEnd 点击 + 挂载/切回前台镜像恢复）
  const startTaskProgressPolling = () => {
    if (taskPollRef.current) return; // 已在轮询
    let inFlight = false;
    taskPollRef.current = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const tsRes = await fetch(`${getApiBase()}/api/system/task-status`);
        if (tsRes.ok) {
          const tsData = await tsRes.json();
          setTaskProgress({ visible: true, tasks: tsData.tasks, allDone: tsData.all_done, progress: tsData.progress ?? 0 });
          // 卡住检测 → 弹窗（仅当前无弹窗且未被「继续等待」抑制）
          const stalled = (tsData.tasks || []).find((t: any) => t?.stalled && !dismissedStallsRef.current.has(t.name));
          setStallModal(prev => prev && (tsData.tasks || []).some((t: any) => t.name === prev.taskId && t.status === 'running' && t.stalled) ? prev : null);
          if (stalled) {
            setStallModal(prev => prev ? prev : { taskId: stalled.name, label: stalled.label || TASK_LABELS[stalled.name] || stalled.name });
          }
          if (tsData.all_done) {
            if (taskPollRef.current) { clearInterval(taskPollRef.current); taskPollRef.current = null; }
            setStallModal(null);
            dismissedStallsRef.current.clear();
            // 全部完成后 3 秒自动消失
            setTimeout(() => setTaskProgress(prev => prev.allDone ? { visible: false, tasks: {}, allDone: true, progress: 100 } : prev), 3000);
          }
        }
      } catch (e) { /* ignore poll errors */ }
      finally { inFlight = false; }
      // 长任务持续刷新，直到后端报告终态或组件卸载。
    }, 2000);
  };

  // 挂载 / 切回前台时镜像恢复：后端还有活就把进度条拉回来（手机切后台重开也能看到）
  const checkTaskStatusOnce = async () => {
    try {
      const res = await fetch(`${getApiBase()}/api/system/task-status`);
      if (res.ok) {
        const d = await res.json();
        if (!d.all_done) {
          setTaskProgress({ visible: true, tasks: d.tasks, allDone: false, progress: d.progress ?? 0 });
          startTaskProgressPolling();
        }
      }
    } catch { /* ignore */ }
  };

  // 「打印日志并终止」/ ❌ 按钮：终止疑似卡死的任务并抓日志
  const cancelStalledTask = async (taskId: string) => {
    setStallModal(null);
    try {
      await fetch(`${getApiBase()}/api/system/task-cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, dump_log: true }),
      });
      showToast('success', '已终止并保存日志到 logs/debug/');
    } catch {
      showToast('error', '终止失败');
    }
  };

  // 「继续等待」：抑制该任务重复弹窗，保留 ❌ 按钮
  const keepWaitingStalled = (taskId: string) => {
    dismissedStallsRef.current.add(taskId);
    setStallModal(null);
  };

  const handleTopicEnd = async (reason = 'manual', sessionId = currentSessionId) => {
    const currentSession = sessionsRef.current.find(session => session.id === sessionId);
    if (!currentSession) return;

    // 防重复点：上一轮整理还在跑时不重复触发
    if (taskProgress.visible && !taskProgress.allDone) {
      showToast('error', '上一轮整理还没跑完，稍等再点');
      return;
    }

    // 显示进度条
    setTaskProgress({ visible: true, tasks: {}, allDone: false, progress: 0 });

    // 调用后端结束话题API
    try {
      const response = await fetch(`${getApiBase()}/api/topic/end/${sessionId}?reason=${encodeURIComponent(reason)}`, {
        method: 'POST'
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      console.log('话题结束成功:', data);

      // 手动点击与约定倒计时可能抵达同一次结束；直接同步已完成的结果。
      if (data.already_ended) {
        setTaskProgress({ visible: false, tasks: {}, allDone: true, progress: 100 });
        if (sessionId === currentSessionIdRef.current) await reloadCurrentSession();
        if (data.topic_status) setTopicStatuses(prev => ({ ...prev, [sessionId]: data.topic_status }));
        await checkTaskStatusOnce();
        return;
      }

      // 深夜"聊到这里"（22:00 后）→ 后端预约次日 00:00 自动结束，话题未立即结束，
      // 不创建 divider、不进进度条轮询。
      if (data && data.reserved) {
        showToast(data.message || '已预约次日 00:00 自动结束');
        setTaskProgress({ visible: false, tasks: {}, allDone: true, progress: 0 });
        return;
      }

      // 后端权威锁：上一轮整理仍在跑 → 拒绝本次结束（多客户端同点也拦得住）
      if (data && data.success === false && data.busy) {
        // 后端权威倒计时可能比前端本地 tick 先一步结束话题。此时 busy
        // 表示整理任务已经在跑，不是失败；同步后端 divider 和进度即可。
        if (reason === 'covenant') {
          if (sessionId === currentSessionIdRef.current) await reloadCurrentSession();
          const syncedTopicStatus = await fetchTopicStatus(sessionId);
          if (syncedTopicStatus) {
            setTopicStatuses(prev => ({ ...prev, [sessionId]: syncedTopicStatus }));
          }
          startTaskProgressPolling();
          return;
        }
        showToast('error', data.message || '上一轮整理还没跑完，稍等再点');
        setTaskProgress({ visible: false, tasks: {}, allDone: true, progress: 0 });
        return;
      }

      // 使用后端返回的 divider_id 创建分隔线消息（确保前后端 ID 一致，持久化不丢）
      const dividerMessage: Message = {
        id: data.divider_id || Date.now().toString(),
        role: 'divider',
        content: '-刚刚聊的话题结束了-',
        timestamp: new Date(),
        thinkingChain: undefined
      };

      // 添加分隔线到当前会话（本地即时显示，后端已持久化）
      const updatedMessages = [...currentSession.messages, dividerMessage];
      await updateSessionMessages(sessionId, updatedMessages);

      // 后台从后端 reload 一次 session，确保前后端数据一致
      if (sessionId === currentSessionIdRef.current) reloadCurrentSession();

      // 更新话题状态
      if (data.topic_status) {
        setTopicStatuses(prev => ({
          ...prev,
          [sessionId]: data.topic_status
        }));
      }

      // 摘要生成是异步的，轮询等待摘要完成（最多等45秒）
      let retries = 0;
      const maxRetries = 15;
      const pollTimer = setInterval(async () => {
        retries++;
        const newSummarized = await fetchSummarizedStatus(sessionId);
        if (newSummarized) {
          if (sessionId === currentSessionIdRef.current) setSummarizedStatus(newSummarized);
          console.log(`[摘要轮询] 第${retries}次: ${newSummarized.total_count}条消息已总结`);
          if (newSummarized.total_count > 0) {
            clearInterval(pollTimer);
            console.log('[摘要轮询] 摘要已完成，停止轮询');
          }
        }
        if (retries >= maxRetries) {
          clearInterval(pollTimer);
          console.warn('[摘要轮询] 轮询超时，摘要可能生成较慢');
        }
      }, 3000);

      // 轮询后端任务进度，等到后端真完成才关（不再有 200s 强制隐藏谎报完成）
      startTaskProgressPolling();
    } catch (error) {
      console.error('结束话题失败:', error);
      setTaskProgress({ visible: false, tasks: {}, allDone: true, progress: 0 });
      // 仍然添加divider，即使后端调用失败
      const dividerMessage: Message = {
        id: Date.now().toString(),
        role: 'divider',
        content: '-刚刚聊的话题结束了-',
        timestamp: new Date(),
        thinkingChain: undefined
      };
      const updatedMessages = [...currentSession.messages, dividerMessage];
      await updateSessionMessages(sessionId, updatedMessages);
    }
  };
  covenantBackendSyncRef.current = async (sessionId: string) => {
    // GET /api/sessions/{id} 的消息来自 SQLite；约定词结束后完全采用该权威快照，
    // 不保留可能在 Android 后台冻结期间滞留的 local-only divider。
    if (sessionId === currentSessionIdRef.current) {
      await reloadCurrentSession({ authoritative: true });
    }
    const syncedTopicStatus = await fetchTopicStatus(sessionId);
    if (syncedTopicStatus) {
      setTopicStatuses(prev => ({ ...prev, [sessionId]: syncedTopicStatus }));
    }
    setTaskProgress(prev => prev.visible
      ? prev
      : { visible: true, tasks: {}, allDone: false, progress: 0 });
    startTaskProgressPolling();
  };

  const clearChat = async () => {
    if (window.confirm('确定要清空当前会话吗？所有消息将被直接删除，不会生成摘要。')) {
      invalidateLocalHistory(currentSessionId);
      try {
        // 调用后端API清空消息（同时清理缓存日志和话题状态）
        const response = await fetch(`${getApiBase()}/api/messages/clear`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            session_id: currentSessionId
          })
        });

        if (!response.ok) {
          throw new Error('清空会话失败');
        }

        // 更新本地消息列表
        await updateSessionMessages(currentSessionId, []);

        // 重置话题状态和已总结消息ID
        setTopicStatuses(prev => ({
          ...prev,
          [currentSessionId]: { is_active: false, start_time: null, message_count: 0, round_count: 0 }
        }));
        setSummarizedStatus({ session_id: currentSessionId, summarized_ids: [], total_count: 0 });
      } catch (error) {
        console.error('清空会话失败:', error);
        alert('清空会话失败');
      }
    }
  };

  const deleteMessage = async (messageId: string) => {
    // 漫想消息也会被 wander_push handler PUT 到 session_manager，需走后端 API 删除
    const session = getCurrentSession();
    const targetMsg = session?.messages.find(m => m.id === messageId);

    if (window.confirm(targetMsg?.isWander ? '确定要删除这条漫想消息吗？' : '确定要删除这条消息吗？')) {
      const mutationKey = messageId;
      if (!session || messageMutationInFlightRef.current.has(mutationKey)) return;
      invalidateLocalHistory(currentSessionId);
      messageMutationInFlightRef.current.add(mutationKey);
      setMutatingMessageIds(current => new Set(current).add(messageId));
      const snapshot = [...session.messages];
      const removedIds = snapshot.filter(message => message.id === messageId || message.eventParentId === messageId)
        .map(message => message.id);
      const removedSet = new Set(removedIds);
      recordRemovedMessages(currentSessionId, removedIds);
      // 这里只做 UI 投影，后端仍是唯一持久化权威；失败时精确补回。
      setSessions(current => current.map(item => item.id === currentSessionId
        ? { ...item, messages: item.messages.filter(message => !removedSet.has(message.id)), updatedAt: new Date() }
        : item));
      let deleteCommitted = false;
      try {
        // 所有消息统一走后端 API 删除（覆盖 session_manager + V2 JSON + SQLite）
        const response = await fetch(`${getApiBase()}/api/messages/delete`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: currentSessionId, message_id: messageId })
        });
        if (!response.ok) throw new Error('删除消息失败');
        deleteCommitted = true;

        const result = await response.json();
        recordRemovedMessages(currentSessionId, result.deleted_message_ids || [messageId]);
        await reloadCurrentSession();
        if (Array.isArray(result.affected_event_types) && result.affected_event_types.length) {
          alert('删除成功，其中包含事件记录。如涉及独立领域，请前往对应领域手动删除实际记录；这里不会撤销已经发生的操作。');
        }

        // 刷新后端话题状态和已总结状态
        const newStatus = await fetchTopicStatus(currentSessionId);
        if (newStatus) {
          setTopicStatuses(prev => ({ ...prev, [currentSessionId]: newStatus }));
        }
        const newSummarized = await fetchSummarizedStatus(currentSessionId);
        if (newSummarized) {
          setSummarizedStatus(newSummarized);
        }
      } catch (error) {
        console.error('删除消息失败:', error);
        if (deleteCommitted) {
          await reloadCurrentSession();
          alert('消息已删除，但后续状态刷新失败；会话已从服务端重新同步。');
          return;
        }
        restoreRemovedMessages(currentSessionId, removedIds);
        setSessions(current => current.map(item => {
          if (item.id !== currentSessionId) return item;
          const currentIds = new Set(item.messages.map(message => message.id));
          const originals = snapshot.filter(message => removedSet.has(message.id) && !currentIds.has(message.id));
          return { ...item, messages: [...item.messages, ...originals].sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime()) };
        }));
        alert('删除消息失败');
      } finally {
        messageMutationInFlightRef.current.delete(mutationKey);
        setMutatingMessageIds(current => {
          const next = new Set(current); next.delete(messageId); return next;
        });
      }
    }
  };

  const revokeMessage = async (userMessageId: string) => {
    if (!window.confirm('确定要撤回这条消息吗？撤回后将删除此消息及其之后的所有消息（包括AI回复和后续对话）。')) {
      return;
    }

    const session = getCurrentSession();
    const mutationKey = userMessageId;
    if (!session || messageMutationInFlightRef.current.has(mutationKey)) return;
    invalidateLocalHistory(currentSessionId);
    const targetIndex = session.messages.findIndex(message => message.id === userMessageId);
    if (targetIndex < 0) return;
    const snapshot = [...session.messages];
    const removedIds = session.messages.slice(targetIndex).map(message => message.id);
    messageMutationInFlightRef.current.add(mutationKey);
    setMutatingMessageIds(current => new Set(current).add(userMessageId));
    recordRemovedMessages(currentSessionId, removedIds);
    const optimisticMessages = session.messages.slice(0, targetIndex);
    setSessions(current => current.map(item => item.id === currentSessionId
      ? { ...item, messages: optimisticMessages, updatedAt: new Date() }
      : item));

    let revokeCommitted = false;
    try {
      const response = await fetch(`${getApiBase()}/api/messages/revoke`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          session_id: currentSessionId,
          user_message_id: userMessageId,
          delete_assistant_reply: true
        })
      });

      if (!response.ok) {
        if (response.status === 404) {
          const authoritative = await sessionApi.getSession(currentSessionId, { limit: MESSAGE_WINDOW });
          if (authoritative && !authoritative.messages.some(message => message.id === userMessageId)) {
            revokeCommitted = true;
            await reloadCurrentSession();
            return;
          }
        }
        const errorData = await response.json();
        throw new Error(errorData.detail || `撤回失败: ${response.status}`);
      }

      revokeCommitted = true;
      const result = await response.json();
      console.log('撤回成功:', result);
      recordRemovedMessages(currentSessionId, result.deleted_message_ids || []);
      await reloadCurrentSession();
      if (Array.isArray(result.affected_event_types) && result.affected_event_types.length) {
        alert('撤回成功，其中包含事件记录。如涉及独立领域，请前往对应领域手动删除实际记录；这里不会撤销已经发生的操作。');
      }

      // V2 SQLite 匹配失败告警：部分 ID 未在后端数据库找到
      if (result.v2_unmatched_ids && result.v2_unmatched_ids.length > 0) {
        const fallbackNote = result.v2_fallback_used ? '\n已通过时间戳兜底清理，数据完整性已恢复。' : '';
        alert(`⚠️ ${result.v2_unmatched_ids.length} 条消息在后端数据库中未匹配到。${fallbackNote}\n未匹配ID: ${result.v2_unmatched_ids.join(', ')}\n请关注此问题，如持续出现请排查数据库同步。`);
      }

      // 刷新话题状态和已总结消息ID（后端已经在撤回时重新计算了）
      const newStatus = await fetchTopicStatus(currentSessionId);
      if (newStatus) {
        setTopicStatuses(prev => ({ ...prev, [currentSessionId]: newStatus }));
      }

      const newSummarized = await fetchSummarizedStatus(currentSessionId);
      if (newSummarized) {
        setSummarizedStatus(newSummarized);
      }
    } catch (error) {
      console.error('撤回消息失败:', error);
      if (revokeCommitted) {
        await reloadCurrentSession();
        alert('消息已撤回，但后续状态刷新失败；会话已从服务端重新同步。');
        return;
      }
      restoreRemovedMessages(currentSessionId, removedIds);
      setSessions(current => current.map(item => {
        if (item.id !== currentSessionId) return item;
        const snapshotIds = new Set(snapshot.map(message => message.id));
        const arrivals = item.messages.filter(message => !snapshotIds.has(message.id));
        return { ...item, messages: [...snapshot, ...arrivals].sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime()) };
      }));
      alert(`撤回失败: ${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      messageMutationInFlightRef.current.delete(mutationKey);
      setMutatingMessageIds(current => {
        const next = new Set(current); next.delete(userMessageId); return next;
      });
    }
  };

  const startEditMessage = (message: Message) => {
    if (editingDraftRef.current?.pending || editingDraftRef.current?.unconfirmed) return;
    if (editingDraftRef.current?.messageId !== message.id) {
      editingDraftRef.current = {messageId: message.id, content: message.content};
    }
    setEditingMessageId(message.id);
  };

  const cancelEditMessage = () => {
    if (editingDraftRef.current?.pending || editingDraftRef.current?.unconfirmed) return;
    editingDraftRef.current = null;
    setEditingMessageId(null);
  };

  const saveEditedMessage = async (messageId: string, content: string) => {
    if (!content.trim()) {
      cancelEditMessage();
      return;
    }

    const currentSession = getCurrentSession();
    if (!currentSession) return;
    const mutationKey = messageId;
    if (messageMutationInFlightRef.current.has(mutationKey)) return;
    const original = currentSession.messages.find(message => message.id === messageId);
    if (!original) return;
    invalidateLocalHistory(currentSessionId);
    const nextContent = content.trim();
    messageMutationInFlightRef.current.add(mutationKey);
    setMutatingMessageIds(current => new Set(current).add(messageId));
    setSessions(current => current.map(item => item.id === currentSessionId
      ? { ...item, messages: item.messages.map(message => message.id === messageId ? { ...message, content: nextContent } : message), updatedAt: new Date() }
      : item));
    cancelEditMessage();

    // 调用后端统一编辑端点，同步更新三个存储通道
    try {
      const response = await fetch(`${getApiBase()}/api/messages/edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: currentSessionId,
          message_id: messageId,
          new_content: nextContent
        })
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || '编辑消息失败');
      }

    } catch (error) {
      console.error('编辑消息失败:', error);
      setSessions(current => current.map(item => item.id === currentSessionId
        ? { ...item, messages: item.messages.map(message => message.id === messageId && message.content === nextContent ? { ...message, content: original.content } : message) }
        : item));
      setEditingMessageId(messageId);
      alert(`编辑消息失败: ${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      messageMutationInFlightRef.current.delete(mutationKey);
      setMutatingMessageIds(current => {
        const next = new Set(current); next.delete(messageId); return next;
      });
    }
  };

  // The backend confirms the replacement before the editor or timeline changes.
  const resendEditedMessage = async (messageId: string, content: string, replacementId: string) => {
    if (mainSendInFlightRef.current || isLoadingRef.current || resendInFlightRef.current.size || !content.trim()) {
      throw new Error('当前仍在处理消息，编辑稿已保留');
    }
    const sourceSession = getCurrentSession();
    if (!sourceSession?.messages.some(message => message.id === messageId)) {
      throw new Error('原消息暂时不可用，编辑稿已保留');
    }
    const sessionId = sourceSession.id;
    if (messageMutationInFlightRef.current.has(messageId)) throw new Error('这条消息正在修改，编辑稿已保留');
    resendInFlightRef.current.add(messageId);
    messageMutationInFlightRef.current.add(messageId);
    setResendingMessageIds(previous => new Set(previous).add(messageId));
    const activity = beginInputActivity('send_preparation', '正在确认重发…');
    try {
      const receipt = await requestMessageResend(sessionId, messageId, content, replacementId);
      const acceptedMessage: Message = {...receipt.message, timestamp: new Date(receipt.message.timestamp)};
      const removed = new Set(receipt.deleted_message_ids);
      invalidateLocalHistory(sessionId);
      recordRemovedMessages(sessionId, receipt.deleted_message_ids);
      setSessions(previous => previous.map(session => session.id === sessionId ? {
        ...session,
        messages: upsertOutgoingMessage(session.messages.filter(message => !removed.has(message.id)), acceptedMessage)
          .sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime()),
        updatedAt: new Date(),
      } : session));
      setEditingMessageId(current => current === messageId ? null : current);
      if (editingDraftRef.current?.messageId === messageId) editingDraftRef.current = null;
      // Generation uses this exact accepted ID; no second delete or full reload.
      const savedSession = {...sourceSession, messages: upsertOutgoingMessage(
        sourceSession.messages.filter(message => !removed.has(message.id)), acceptedMessage)};
      endInputActivity(activity);
      const send = handleSend(acceptedMessage.content, [], undefined, undefined, savedSession, acceptedMessage);
      if (send === false) {
        showToast('error', '修改后的消息已保存，回复尚未开始，可以稍后重试');
      } else {
        await send.catch(() => showToast('error', '修改后的消息已保存，回复未完成，可以重试'));
      }
      // Display-only status refresh cannot delay admission or the provider call.
      void fetchTopicStatus(sessionId).then(status => {
        if (status) setTopicStatuses(previous => ({...previous, [sessionId]: status}));
      }).catch(() => {});
      void fetchSummarizedStatus(sessionId).then(status => {
        if (status && currentSessionIdRef.current === sessionId) setSummarizedStatus(status);
      }).catch(() => {});
    } finally {
      endInputActivity(activity);
      resendInFlightRef.current.delete(messageId);
      messageMutationInFlightRef.current.delete(messageId);
      setResendingMessageIds(previous => { const next = new Set(previous); next.delete(messageId); return next; });
    }
  };

	// 清理7天前的对话记录
	const clearOldMessages = async () => {
	    if (!window.confirm('确定要清理7天前的对话记录吗？最近7天的消息将被保留。')) {
	      return;
	    }
	
	    const cutoff = new Date();
	    cutoff.setDate(cutoff.getDate() - 6);
	    cutoff.setHours(0, 0, 0, 0);
	
	    const updatedSessions = await Promise.all(sessions.map(async (session) => {
	      const filteredMessages = session.messages.filter(msg => {
	        return msg.timestamp >= cutoff;
	      });
	
	      if (filteredMessages.length !== session.messages.length) {
	        await updateSessionMessages(session.id, filteredMessages);
	        return {
	          ...session,
	          messages: filteredMessages,
	          updatedAt: new Date()
	        };
	      }
	
	      return session;
	    }));
	
	    setSessions(updatedSessions);
	    alert('7天前的对话记录已清理完成！');
	};

  return (
    <div className={`app ${nightMode ? 'night-mode' : ''}`} data-view={view} data-platform={getPlatform()}>
      <CommerceActionRequests />
      {nightMode && <FloatingHearts />}
      <header className={`header${('')}`}>
        <div className="header-left">
          <button className="mobile-sidebar-toggle" onClick={() => setShowMobileSidebar(!showMobileSidebar)} title="菜单">☰</button>
          {((
              <>
                {getPlatform() !== 'mobile' && <h1>MIRROW</h1>}
              </>
            ))
          }
          {/* 移动端 → 页面菜单按钮（后宫模式隐藏） */}
          {<div className="mobile-more-wrapper">
             <button
               className={`mobile-more-btn${('')}`}
               onClick={(e) => { e.stopPropagation(); setShowMobileMoreMenu(!showMobileMoreMenu); }}
               title="打开手机中枢"
               aria-label="打开手机中枢"
               aria-expanded={showMobileMoreMenu}
             >
               ✦
               
             </button>
             <MobileHubMenu
               open={showMobileMoreMenu}
               view={view}
               onNavigate={(next) => {
                 if (next === 'affect') setShowAffectBoard(true);
                 else setView(next);
               }}
               onClose={() => setShowMobileMoreMenu(false)}
               
               onOpenTasks={() => {
                 calendar.setTaskPanelOpen(!calendar.taskPanelOpen);
                 setSettingsOpen(false);
               }}
               onOpenSettings={() => {
                 setSettingsOpen(!settingsOpen);
                 calendar.setTaskPanelOpen(false);
               }}
               onToggleNight={nightControl.toggle}
               onToggleMusic={() => openMusic('player')}
               onTogglePhoneBrowse={phoneBrowseControl.toggle}
               nightMode={nightMode}
               nightModePending={nightControl.pending}
               phoneBrowseModePending={phoneBrowseControl.pending}
               musicMode={musicMode}
               phoneBrowseMode={phoneBrowseMode}
               
             />
           </div>}
           {getPlatform() === 'mobile' && <span className="mobile-mirrow-logo">MIRROW</span>}
           <nav className="tabs" style={(undefined)}>
            <button
              className={`tab ${view === 'chat' ? 'active' : ''}`}
              onClick={() => { setView('chat') }}
            >
              聊天
            </button>
            <button
              className={`tab ${view === 'persona' ? 'active' : ''}`}
              onClick={() => setView('persona')}
            >
              人设卡
            </button>
            <button
              className={`tab ${view === 'monitor' ? 'active' : ''}`}
              onClick={() => setView('monitor')}
            >
              监控台
            </button>
            <button
              className={`tab ${view === 'memory' ? 'active' : ''}`}
              onClick={() => setView('memory')}
            >
              记忆库
            </button>
             <button
               className={`tab ${view === 'events' ? 'active' : ''}`}
               onClick={() => setView('events')}
             >
               事件纪
             </button>
             <button className={`tab ${['machineLife', 'workshop', 'social', 'lounge'].includes(view) ? 'active' : ''}`} onClick={() => setView('machineLife')}>机生活</button>
            <button
              className={`tab ${view === 'selfbook' ? 'active' : ''}`}
              onClick={() => { setShowPersonaEvo(false); setView('selfbook'); }}
            >
              认知书
            </button>
          </nav>
        </div>
        <div className="header-controls" style={(undefined)}>
          {<SocialInbox onOpenFeed={()=>{setLifeChild(false);setView('social');}}/>}
          {getPlatform() === 'mobile'
            ? <div className="header-mood-peek">
                <button type="button" className="header-mood-badge" aria-label={`查看 AI 当前主心情：${moodDisplay.label}`} aria-expanded={showMoodPeek} onClick={() => setShowMoodPeek(value => !value)} onBlur={() => window.setTimeout(() => setShowMoodPeek(false), 100)}>{moodDisplay.emoji}</button>
                {showMoodPeek && <div className="header-mood-peek-card" role="status"><strong>{moodDisplay.label}</strong>{moodDisplay.text && <span>{moodDisplay.text}</span>}<small>完整心情在手机中枢查看</small></div>}
              </div>
            : <button type="button" className="header-mood-badge" title={moodDisplay.text || '打开心情面板'} aria-label="打开心情面板" onClick={() => setShowAffectBoard(true)}>{moodDisplay.emoji}</button>}
          <button
            className="task-toggle"
            onClick={() => { calendar.setTaskPanelOpen(!calendar.taskPanelOpen); setSettingsOpen(false); }}
            title="定时任务"
          >
            📋
          </button>
          <button
            className="settings-toggle"
            onClick={() => { setSettingsOpen(!settingsOpen); calendar.setTaskPanelOpen(false); }}
            title="打开设置"
          >
            ⚙️
          </button>
          <button
            className="theme-toggle"
            onClick={nightControl.toggle}
            disabled={nightControl.pending}
            title={nightMode ? '退出深夜模式' : '开启深夜模式'}
          >
            {nightMode ? '❤️‍🔥' : '🤍'}
          </button>
          <button
            className={`music-toggle${musicMode ? ' active' : ''}`}
            onClick={() => openMusic('player')}
            title="打开音乐中枢"
          >
            🎵
          </button>
          <button
            className={`phone-browse-toggle${phoneBrowseMode ? ' active' : ''}`}
            onClick={phoneBrowseControl.toggle}
            disabled={phoneBrowseControl.pending}
            title={phoneBrowseMode ? '退出刷手机模式' : '开启刷手机模式'}
          >
            📱
          </button>
        </div>
      </header>

      <TaskProgressPanel progress={taskProgress} onCancel={cancelStalledTask} />

      {/* 设置面板 */}
      {settingsOpen && (
        <div className="settings-backdrop" onClick={() => setSettingsOpen(false)}>
        <section className="settings-panel" role="dialog" aria-modal="true" aria-label="MIRROW 设置" onClick={event=>event.stopPropagation()}>
          <div className="settings-header">
            <div><span className="settings-eyebrow">MIRROW / CONTROL ROOM</span><h3>设置</h3><p>调整 AI 的身体能力、连续性与外部行动。</p></div>
            <button
              className="settings-close"
              onClick={() => setSettingsOpen(false)}
              title="关闭设置"
            >
              ×
            </button>
          </div>
          <div className="settings-layout">
          <SettingsNavigation active={settingsTab} onChange={setSettingsTab}/>
          <div className="settings-content">
            {settingsTab === 'general' && (
              <SettingsGeneralTab
                autoStart={autoStart}
                autoStartLoaded={autoStartLoaded}
                toggleAutoStart={toggleAutoStart}
                autoPlayVoice={autoPlayVoice}
                toggleAutoPlayVoice={toggleAutoPlayVoice}
                clearChat={clearChat}
                clearOldMessages={clearOldMessages}
                covenantSettings={covenant.settings}
                saveCovenantSettings={covenant.saveSettings}
                longTextCardFullContext={longTextCardFullContext}
                onLongTextCardFullContextChange={async value => { const previous = longTextCardFullContext; setLongTextCardFullContext(value); try { const response = await fetch(`${getApiBase()}/api/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: 'longTextCardFullContext', value }) }); if (!response.ok) throw new Error(`HTTP ${response.status}`); } catch { setLongTextCardFullContext(previous); } }}
              />
            )}

            {settingsTab === 'api' && <ApiSettingsTab />}
            
            {settingsTab === 'interaction' && (
              <SettingsInteractionTab
                wanderStatus={wanderStatus}
                fetchWanderStatus={fetchWanderStatus}
                phoneBrowseInterval={phoneBrowseInterval}
                setPhoneBrowseInterval={setPhoneBrowseInterval}
                phoneBrowseMode={phoneBrowseMode}
                widgetDiag={widgetDiag}
                showToast={showToast}
              />
            )}

            {settingsTab === 'permission' && (
              <SettingsPermissionTab
                a11yStatus={a11yStatus}
                setA11yStatus={setA11yStatus}
              />
            )}

            {settingsTab === 'truncation' && (
              <TruncationSettings />
            )}
            
            {settingsTab === 'user-status' && (
              <UserStatusSettings userStatusMode={userStatusMode} setUserStatusMode={setUserStatusMode} />
            )}
            {settingsTab === 'commerce' && <CommerceSettings />}
            {settingsTab === 'rift' && <RiftArchiveSettings />}
          </div>
          </div>
        </section>
        </div>
      )}

      {calendar.taskPanelOpen && (
        <CalendarPanel calendar={calendar} onClose={() => calendar.setTaskPanelOpen(false)} />
      )}
      {view === 'chat' ? (
        <div className="container">
          {showMobileSidebar && (
            <div className="sidebar-backdrop" onClick={() => setShowMobileSidebar(false)} />
          )}
          <div className={`sidebar ${showMobileSidebar ? 'mobile-visible' : ''}`}>
            {/* 漫想状态与关闭按钮共享首行；状态尚未返回时仍保留稳定的占位行。 */}
            <div className={`sidebar-top-row${wanderStatus ? ' has-wander-status' : ' no-wander-status'}`}>
              <div className="sidebar-wander-status">
                {wanderStatus || loungeStatus.busy ? (
                  <div className={`wander-status-indicator ${loungeStatus.busy || wanderStatus?.is_wander_mode_active ? 'active' : ''}`}>
                    <span className="wander-avatar-wrap"><span className="wander-avatar">
                      {persona.avatar.startsWith('data:')
                        ? <img src={persona.avatar} alt="AI头像" className="avatar-img" />
                        : persona.avatar}
                    </span><span className="wander-avatar-badge" aria-hidden="true">{presence.icon}</span></span>
                    <span className="wander-status-text">
                      {loungeStatus.busy ? loungeStatus.label : presence.label}
                      {presence.detail && <small>{presence.detail}</small>}
                    </span>
                  </div>
                ) : (
                  <span className="sidebar-status-fallback" aria-hidden="true" />
                )}
              </div>
              <button className="sidebar-close-btn" onClick={() => setShowMobileSidebar(false)} title="关闭菜单" aria-label="关闭菜单">✕</button>
            </div>

            {/* 工具栏入口 — 独立于漫想状态 */}
            <div className="wander-status-display">
              <div className="wander-status-indicator">
                <button
                  className={`ledger-toggle ${showLedger ? 'active' : ''}`}
                  onClick={() => setShowLedger(!showLedger)}
                  title="记账本"
                >📒</button>
                
                <button
                  className={`ledger-toggle ${showWanderLog ? 'active' : ''}`}
                  onClick={() => setShowWanderLog(!showWanderLog)}
                  title="漫想日志"
                  style={{marginLeft:4}}
                >💭</button>
                <button
                  className={`ledger-toggle ${showSentinel ? 'active' : ''}`}
                  onClick={() => setShowSentinel(!showSentinel)}
                  title="哨兵"
                  style={{marginLeft:4}}
                >🛡️</button>
                <button
                  className={`ledger-toggle ${showBookmark ? 'active' : ''}`}
                  onClick={() => {
                    const next = !showBookmark;
                    setShowBookmark(next);
                    if (next) bookmark.openPanel();
                  }}
                  title="收藏夹"
                  style={{marginLeft:4}}
                >⭐</button>
                
              </div>
              {showLedger && <LedgerPanel onClose={() => setShowLedger(false)} />}
              
              <PerceptionPanel show={showSentinel} onClose={() => setShowSentinel(false)} onTriggerSleepPrompt={() => setShowSleepPrompt(true)} />
              {showImport && <ImportPanel onClose={() => setShowImport(false)} />}
              {showBookmark && <BookmarkPanel bookmark={bookmark} onClose={() => setShowBookmark(false)} />}
            </div>

              {showWanderLog && (
                <WanderLogPanel
                  onClose={() => setShowWanderLog(false)}
                  onOpenWishBoard={() => {
                    setShowWanderLog(false);
                    setShowMobileSidebar(false);
                    openWishBoard();
                  }}
                />
              )}

              


            {/* 用户状态选择器 */}
            <UserStatusPanel
              currentStatus={userStatus}
              onStatusChange={handleStatusChange}
              savedCustomText={wanderStatus?.user_status_custom_text || ''}
              presets={wanderStatus?.user_status_presets || {}}
              mode={userStatusMode}
              statusMeta={statusMeta}
            />

            {/* 会话管理区域 */}
            <div className="sidebar-section">
              <div className="session-header">
                <h3>会话管理</h3>
                <div className="session-header-btns">
                  <button
                    className="reload-sessions-btn"
                    onClick={async () => {
                      console.log('[reloadSessions] Reloading authoritative sessions...');
                      setSessionsReload(n => n + 1);
                    }}
                    title="重新加载会话列表"
                    disabled={isLoadingSessions}
                  >
                    ↻
                  </button>
                  
                  
                  <button
                    className="add-session-btn"
                    onClick={addSession}
                    title="添加新会话"
                    disabled={sessions.length >= 10}
                  >
                    +
                  </button>
                </div>
              </div>
              <div className="session-list">
                
                {isLoadingSessions ? (
                  <div className="loading-sessions">
                    <div className="typing-indicator">
                      <span></span>
                      <span></span>
                      <span></span>
                    </div>
                    加载会话中...
                  </div>
                ) : (
                  sessions.map(session => (
                    <div
                      key={session.id}
                      className={`session-item ${
                        chatView === 'normal' && session.id === currentSessionId
                          ? 'active' : ''
                      }`}
                      onClick={(e) => {
                        if (editingSessionId === session.id || (e.target as HTMLElement).closest('.edit-session-btn')) {
                          return;
                        }
                        void (async()=>{
                          
                          if (chatView !== 'normal') { setChatView('normal'); }
                          await switchSession(session.id);
                        })();
                      }}
                    >
                      <div className="session-info">
                        {editingSessionId === session.id ? (
                          <SessionTitleEditor
                            initialTitle={session.title}
                            sessionId={session.id}
                            onSave={editSessionTitle}
                            onCancel={() => setEditingSessionId(null)}
                          />
                        ) : (
                          <div
                            className="session-title"
                            onDoubleClick={() => {
                              setEditingSessionId(session.id);
                            }}
                          >
                            {session.title}
                          </div>
                        )}
                        <div className="session-meta">
                          {session.isPrimary && session.totalMessages !== undefined
                            ? `${session.totalMessages.toLocaleString()} 条消息 · 已加载 ${session.messages.length.toLocaleString()}`
                            : `${session.messages.length} 条已加载 · ${new Date(session.updatedAt).toLocaleDateString()}`}
                        </div>
                        {session.isPrimary && fullHistoryProgress && (
                          <div className="session-cache-progress">
                            {fullHistoryProgress.complete ? '本机已缓存全部' : '本机预读'}：
                            {fullHistoryProgress.loaded.toLocaleString()} / {fullHistoryProgress.total.toLocaleString()}
                          </div>
                        )}
                      </div>
                      <div className="session-item-actions">
                        {!session.isPrimary && <button
                          className={`lock-session-btn ${session.locked ? 'locked' : ''}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleSessionLock(session.id);
                          }}
                          title={session.locked ? '解锁会话' : '锁定会话'}
                        >
                          {session.locked ? '🔒' : '🔓'}
                        </button>}
                        <button
                          className="edit-session-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingSessionId(session.id);
                          }}
                          title="编辑会话标题"
                        >
                          ✎
                        </button>
                        {session.isPrimary ? <button
                          className="preload-session-btn"
                          type="button"
                          onClick={(e) => { e.stopPropagation(); toggleFullHistoryPrefetch(session.id); }}
                          title={prefetchingSessionId === session.id
                            ? '暂停本机全量预读' : '在当前设备后台逐页预读全部消息'}
                          aria-label={prefetchingSessionId === session.id ? '暂停全量预读' : '全量预读消息'}
                        >
                          {prefetchingSessionId === session.id ? 'Ⅱ' : '↓'}
                        </button> : <button
                          className="delete-session-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteSession(session.id);
                          }}
                          title={session.locked ? '会话已上锁，无法删除' : '删除会话'}
                          disabled={sessions.length <= 1 || session.locked}
                        >
                          ×
                        </button>}
                      </div>
                    </div>
                  ))
                )}
              </div>
              <div className="session-stats">
                共 {sessions.length}/10 个会话
              </div>
              {/* 话题状态显示区域 */}
              <div className="topic-status-area">
                {currentSessionId && topicStatuses[currentSessionId] && (
                  <div className="current-topic-status">
                    {topicStatuses[currentSessionId].is_active ? (
                      <>
                        <span className="topic-indicator active">●</span>
                        <span className="topic-info">
                          话题: {topicStatuses[currentSessionId].start_time
                            ? formatTimestamp(new Date(topicStatuses[currentSessionId].start_time))
                            : '未知'}开始
                          · {topicStatuses[currentSessionId].message_count}条消息
                          · {topicStatuses[currentSessionId].round_count}轮
                        </span>
                      </>
                    ) : (
                      <span className="topic-info inactive">暂无活跃话题</span>
                    )}
                  </div>
                )}
              </div>
            </div>





          </div>

          <div className="main">
            <div className={`chat-container${('')}`}>
              {/* 会话信息栏（后宫模式隐藏） */}
              <div className={`session-title-bar session-title-bar-plain${getPlatform() === 'mobile' ? ' session-title-bar-mobile' : ''}`} style={(false || false) ? {display:'none'} : undefined}>
                {getPlatform() !== 'mobile' && (
                  <div className="session-actions">
                    <span className="session-title-info">
                      已加载 {getCurrentMessages().length} 条消息
                    </span>
                    <button className="export-btn" onClick={exportMessagesToMarkdown} title="导出当前已加载的消息为 Markdown" disabled={getCurrentMessages().length === 0}>
                      📥 导出已加载
                    </button>
                    <button className="import-btn" onClick={() => setShowImport(true)} title="从 SQLite 文件导入消息">
                      📤 导入
                    </button>
                  </div>
                )}
              </div>

              

              <div data-conversation-surface ref={messagesRef} className={`messages${chatBackgroundSrc ? ' has-chat-background' : ''}`} style={{ position: 'relative', display: (chatView !== 'normal') ? 'none' : undefined }}>
                {chatBackgroundSrc && (
                  <div className="chat-background-layer" aria-hidden="true">
                    <img src={chatBackgroundSrc} alt="" style={{ opacity: persona.chatBackgroundOpacity ?? 0.42 }} />
                  </div>
                )}
                <ConversationCovenantOverlay
                  countdown={covenant.formattedCountdown}
                  activationNotice={covenant.activationNotice}
                  cancelPromptOpen={covenant.cancelPromptOpen}
                  onOpenCancelPrompt={covenant.openCancelPrompt}
                  onCloseCancelPrompt={covenant.closeCancelPrompt}
                  onConfirmCancel={covenant.cancelCountdown}
                />
                <Virtuoso
                  ref={virtuosoRef}
                  followOutput={historyView ? false : true}
                  // 超大预渲染会让手机端在高度重测时大幅校正 scrollTop，表现为
                  // 触底后突然回弹。日常聊天只保留小幅缓冲；长截图另走专用模式。
                  increaseViewportBy={getPlatform() === 'mobile' ? { top: 480, bottom: 480 } : 0}
                  atBottomThreshold={120}
                  computeItemKey={(index) => {
                    const items = ((timelineMessages));
                    const item = items[index];
                    const base = item?.id || index;
                    // 正在编辑的消息要换 key，否则 Virtuoso 不重渲染
                    if (item && editingMessageId === String(item.id)) return `${base}_editing`;
                    return base;
                  }}
                  style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 1 }}
                  data={((timelineMessages))}
                  atBottomStateChange={handleAtBottomChange}
                  onMouseUp={(e) => {
                    const sel = window.getSelection();
                    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
                      setSelectionFlag(null); setFlagSubmitted(false); return;
                    }
                    const text = sel.toString().trim();
                    const range = sel.getRangeAt(0);
                    const rect = range.getBoundingClientRect();
                    const container = (e.target as HTMLElement).closest('.message');
                    if (!container) { setSelectionFlag(null); return; }
                    setFlagSubmitted(false);
                    setSelectionFlag({ text, x: rect.left + rect.width / 2, y: rect.top - 36 });
                  }}
                  components={{
                    Header: () => <div style={{ height: getPlatform() === 'mobile' ? 0 : '1rem' }} />,
                    Footer: () => (
                      <div>
                        {historyView?.hasMoreAfter && (
                          <div className="history-load-newer">
                            <button type="button" onClick={loadNewerMessages} disabled={loadingNewer}
                              aria-label="继续加载较新消息">
                              ↓ {loadingNewer ? '正在加载…' : '继续加载较新消息'}
                            </button>
                          </div>
                        )}
                        <div style={{ height: '1rem' }} />
                      </div>
                    ),
                  }}
                  itemContent={(_index, message) => {
                    
                    
                    const chatRow = (
                      <MessageRow
                        key={message.id}
                        message={message}
                        userAvatar={userAvatar}
                        personaAvatar={persona.avatar}
                        personaName={persona.name}
                        currentSessionId={currentSessionId || ''}
                        
                        
                        
                        isEditing={editingMessageId === message.id}
                        isThinkingExpanded={expandedThinkingId === message.id}
                        isHighlighted={highlightedMsgId === message.id}
                        isSummarized={summarizedStatus?.summarized_ids.includes(message.id) ?? false}
                        formatTimestamp={formatTimestamp}
                        onToggleThinking={() => setExpandedThinkingId(expandedThinkingId === message.id ? null : message.id)}
                        onStartEdit={() => startEditMessage(message)}
                        onDelete={() => deleteMessage(message.id)}
                        onRevoke={() => revokeMessage(message.id)}
                        isBookmarked={bookmark.bookmarkedIds.has(message.id)}
                        onBookmark={() => bookmark.handleBookmarkMessage(message)}
                        onOpenWishBoard={openWishBoard}
                        onOpenSocialFeed={() => {setLifeChild(false); setView('social');}}
                        
                        onOpenWorkshop={jobId => {setWorkshopJobId(jobId); setView('workshop');}}
                        onOpenMoodCheckin={() => setShowAffectQuickCheckin(true)}
                        onLongTextCardUpdated={(messageId, card) => setSessions(previous => previous.map(session => session.id === currentSessionId ? { ...session, messages: session.messages.map(item => item.id === messageId ? { ...item, longTextCard: card } : item) } : session))}
                        onSaveEdit={saveEditedMessage}
                        onResendEdit={resendEditedMessage}
                        editDraft={editingDraftRef.current?.messageId === message.id ? editingDraftRef.current ?? undefined : undefined}
                        isResending={inputBlocked || resendingMessageIds.has(message.id) || mutatingMessageIds.has(message.id)}
                        onCancelEdit={cancelEditMessage}
                      />
                    );
                    if (message.role !== 'event') return chatRow;
                    return (
                      <EventRowShell key={message.id} className="activity-event-shell" canDelete={!message.eventParentId} onDelete={() => deleteMessage(message.id)}>
                        {chatRow}
                      </EventRowShell>
                    );
                  }}
                />
                {(hiddenCount > 0 || (historyView?.hasMoreBefore ?? getCurrentSession()?.hasMoreHistory ?? false)) && (
                  <button
                    onClick={loadMoreMessages}
                    style={{
                      position: 'absolute', top: 12, right: 20, zIndex: 10,
                      padding: '0.3rem 0.6rem', opacity: 0.6,
                      background: 'var(--bg-primary)', border: '1px solid var(--border-color)',
                      borderRadius: '50%', cursor: 'pointer', fontSize: '0.9rem',
                      color: 'var(--text-secondary)', lineHeight: 1,
                    }}
                    title={hiddenCount > 0
                      ? `加载更早消息（+${MESSAGE_WINDOW}条，本机还剩${hiddenCount}条）`
                      : '加载更早消息'}
                  >
                    ↑
                  </button>
                )}
                {<ConversationQuickTools revealToken={highlightedMsgId} onSearch={() => setShowMsgSearch(s => !s)} />}
                {showMsgSearch && (
                  <MessageSearchPanel
                    sessionId={currentSessionId}
                    personaName={persona.name}
                    formatTimestamp={formatTimestamp}
                    onLocate={locateMessage}
                    onClose={() => setShowMsgSearch(false)}
                  />
                )}
                {(showScrollToBottom || historyView) && (
                  <button
                    className="scroll-to-bottom-btn"
                    style={{ position: 'absolute', bottom: 20, right: 20, zIndex: 10 }}
                    onClick={scrollToBottom}
                    title="返回最新消息"
                  >
                    ↓<span className="scroll-to-bottom-text"> 最新消息</span>
                  </button>
                )}
              </div>
              {/* 记入世界书浮动按钮 */}
              {selectionFlag && !flagSubmitted && (
                <button
                  className="wb-flag-btn"
                  style={{ left: selectionFlag.x, top: selectionFlag.y, position: 'fixed', transform: 'translateX(-50%)' }}
                  onClick={async () => {
                    await fetch(`${API_BASE}/api/world-book/flags`, {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ selected_text: selectionFlag.text, session_id: currentSessionId || '' }),
                    });
                    setFlagSubmitted(true);
                    setTimeout(() => { setSelectionFlag(null); setFlagSubmitted(false); }, 2000);
                  }}
                >
                  {flagSubmitted ? '✓ 已标记' : '📖 记入世界书'}
                </button>
              )}

              {chatView === 'normal' && <ChatActivityStatus
                label={inputBlocked ? inputActivityLabel : chatTurnPhase === 'syncing' ? '正在同步消息，可继续发送' : undefined}
                background={!inputBlocked}
              />}
              {chatView === 'normal' && ((<ChatInput
              key={currentSessionId}
              isLoading={inputBlocked}
              draftDisabled={activeInputActivities.some(activity => activity.kind === 'send_preparation' || activity.kind === 'voice_recognition')}
              model={model}
              imageModel={imageModel}
              useMemory={useMemory}
              onSend={((text,images,voice,card)=>handleSend(text,images,voice,card))}
              sessionId={currentSessionId||undefined}
              onTopicEnd={handleTopicEnd}
              attachmentsEnabled={true}
              onVoiceProcessingChange={handleVoiceProcessingChange}
            />))}
            </div>
          </div>
        </div>
      ) : view === 'persona' ? (
        <PersonaPage
          persona={persona}
          syncStatus={personaSyncStatus}
          userAvatar={userAvatar}
          
          onPersonaChange={setPersona}
          onUserAvatarChange={setUserAvatar}
          onAiAvatarUpload={handleAiAvatarUpload}
          onUserAvatarUpload={handleUserAvatarUpload}
          
          
          onBack={() => setView('chat')}
        />
      ) : null}

      {/* 监控台 - 独立全屏页面 */}
      {view === 'monitor' && (
        <Suspense fallback={<div className="monitor-loading">加载监控台...</div>}>
          <MonitorPage
            onBack={() => setView('chat')}
            onFetchBrainArchitecture={fetchBrainArchitecture}
          />
        </Suspense>
      )}
        {showBrainArch && (
          <div className="ledger-overlay" onClick={() => setShowBrainArch(false)}>
            <div className="ledger-modal brain-arch-modal" onClick={e => e.stopPropagation()} style={{maxWidth:'800px',maxHeight:'85vh',overflowY:'auto'}}>
              <div className="ledger-title">
                <span>🧠 我的身体</span>
                <button className="ledger-close-btn" onClick={() => setShowBrainArch(false)}>✕</button>
              </div>
              <Suspense fallback={<p role="status">正在读取身体说明…</p>}><BodyKnowledge /></Suspense>
            </div>
          </div>
        )}

      {/* 睡眠数据上传弹窗 */}
      {showSleepPrompt && (
        <div className="ledger-overlay" onClick={() => {}}>
          <div className="ledger-modal" onClick={e => e.stopPropagation()} style={{maxWidth:'380px'}}>
            <div className="ledger-title">
              <span>🌙 昨晚睡得怎么样？</span>
              <button className="ledger-close-btn" onClick={handleSleepSkip}>✕</button>
            </div>
            <div style={{padding:'16px',display:'flex',flexDirection:'column',gap:'12px'}}>
              <label style={{display:'block',width:'100%',padding:'12px',textAlign:'center',
                border:'2px dashed #888',borderRadius:'8px',cursor:'pointer',background:'#1a1a2e'}}>
                {sleepUploading ? '⏳ 分析中...' : '📤 上传睡眠截图'}
                <input type="file" accept="image/*" style={{display:'none'}}
                  onChange={e => { const f = e.target.files?.[0]; if (f) handleSleepUpload(f); }}
                  disabled={sleepUploading} />
              </label>
              <button onClick={handleOpenHuaweiHealth}
                style={{padding:'12px',border:'1px solid #888',borderRadius:'8px',cursor:'pointer',
                  background:'transparent',color:'inherit',fontSize:'0.9em'}}>
                📱 打开华为健康
              </button>
              <div style={{fontSize:'0.75em',color:'#888',textAlign:'center'}}>
                在华为健康中：睡眠详情 → 右上角 ⋮ → 分享 → 保存到本地 → 回到 MIRROW 上传
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 记忆库可视化 - 独立全屏页面 */}
      {view === 'memory' && <MemoryView onBack={() => setView('chat')} />}

      {/* 事件纪 - 独立全屏页面 */}
      {view === 'events' && (
        <Suspense fallback={<div className="loading-page">加载事件纪...</div>}>
          <EventChroniclePage
            onBack={() => { setView('chat') }}
            pendingMemorial={calendar.pendingMemorial}
            onAcceptMemorial={calendar.acceptMemorial}
            onRejectMemorial={calendar.rejectMemorial}
          />
        </Suspense>
      )}

      

      {/* AI 与人类伙伴的私有朋友圈；未读通知由页面先展示再确认已读 */}
      {view === 'social' && (
        <Suspense fallback={<div className="loading-page">加载共域...</div>}>
          <SocialFeedPage onBack={() => {setView(lifeChild ? 'machineLife' : 'chat'); setLifeChild(false);}} />
        </Suspense>
      )}

      
      {view === 'machineLife' && <Suspense fallback={<div className="loading-page">加载机生活...</div>}><MachineLifePage   onBack={() => setView('chat')} onSocial={() => {setLifeChild(true); setView('social');}} onLounge={() => {setLifeChild(true); setView('lounge');}}  onMusic={() => openMusic('home')}   onWorkshop={() => {setLifeChild(true); setWorkshopJobId(''); setView('workshop');}} dark={nightMode} /></Suspense>}
      
      
      {view === 'workshop' && <Suspense fallback={<div className="loading-page">加载工作坊...</div>}><WorkshopPage onBack={() => {setView('machineLife'); setLifeChild(false);}} initialJobId={workshopJobId} dark={nightMode} /></Suspense>}
      {view === 'lounge' && <Suspense fallback={<div className="loading-page">加载会客室...</div>}><LoungePage onBack={() => {setView(lifeChild ? 'machineLife' : 'chat'); setLifeChild(false);}} dark={nightMode} /></Suspense>}

      

      {/* 世界书 - 独立全屏页面 */}
      {view === 'worldbook' && (
        <Suspense fallback={<div className="loading-page">加载世界书...</div>}>
          <CognitionPage initialBook="world" onBack={() => { setView('chat') }} />
        </Suspense>
      )}

      {/* 自我书 - 独立全屏页面 */}
      {view === 'selfbook' && (
        <Suspense fallback={<div className="loading-page">加载认知书...</div>}>
          <CognitionPage initialBook={showPersonaEvo ? 'other' : 'self'} initialSection={showPersonaEvo ? 'legacy' : 'content'} onBack={() => { setShowPersonaEvo(false); setView('chat'); }} onMachineLife={() => setView('machineLife')} />
        </Suspense>
      )}

      {/* 卡住检测弹窗（任务连续 5min 无进展） */}
      {stallModal && (
        <div className="ledger-overlay" onClick={() => keepWaitingStalled(stallModal.taskId)}>
          <div className="ledger-modal" style={{ maxWidth: 440 }} onClick={e => e.stopPropagation()}>
            <div style={{ padding: '16px 18px 8px', fontWeight: 600, fontSize: '1.05rem' }}>整理可能卡住了</div>
            <div style={{ padding: '4px 18px 12px', lineHeight: 1.7 }}>
              整理「{stallModal.label}」时较长时间未报告新进展，任务仍在运行。是否继续等待？
              <br />
              <span style={{ fontSize: '0.85em', opacity: 0.7 }}>选「打印日志并终止」会把本次日志存到 logs/debug/ 方便排查。</span>
            </div>
            <div style={{ display: 'flex', gap: 10, padding: '4px 18px 18px', justifyContent: 'flex-end' }}>
              <button onClick={() => cancelStalledTask(stallModal.taskId)}
                style={{ padding: '8px 14px', borderRadius: 8, border: '1px solid #e57373', background: '#fff', color: '#d32f2f', cursor: 'pointer' }}>
                打印日志并终止
              </button>
              <button onClick={() => keepWaitingStalled(stallModal.taskId)}
                style={{ padding: '8px 14px', borderRadius: 8, border: 'none', background: '#7c4dff', color: '#fff', cursor: 'pointer' }}>
                继续等待
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 通知卡片（浮动在所有页面之上）── */}
      {calendar.notificationCards.map(card => (
        <DraggableCard
          key={card.id}
          title={card.title}
          message={card.message}
          initialX={card.x}
          initialY={card.y}
          onClose={() => calendar.dismissNotificationCard(card.id)}
        />
      ))}


      <WishBoardModal
        open={showWishBoard}
        focus={wishBoardFocus}
        onClose={() => { setShowWishBoard(false); setWishBoardFocus(null); }}
      />

      <AffectBoard
        open={showAffectBoard}
        onClose={() => setShowAffectBoard(false)}
      />
      <AffectQuickCheckin
        open={showAffectQuickCheckin}
        onClose={() => setShowAffectQuickCheckin(false)}
        onSubmitted={() => { void fetchMood(); }}
      />

      <MusicHost onAttach={() => setView('chat')} />
      <ToastContainer />

    </div>
  );
}

/* ── 事件纪组件已移至 pages/EventChroniclePage.tsx ── */


{/* 世界书页面已移至 pages/WorldBookPage.tsx */}

export default App;

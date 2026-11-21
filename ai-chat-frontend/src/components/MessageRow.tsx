
import EssayCard from './EssayCard';
import { ReadingPicture } from './ReadingIllustrations';
import MusicCard from './music/MusicCard';
import AsyncMusicCard from './music/AsyncMusicCard';
import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { getApiBase } from '../config';
import MessageEditArea, {type MessageEditDraft} from './MessageEditArea';
import type { Message, ToolCallRecord } from '../types';
import VoicePlayer, { extractVoiceTracks, SingleVoicePlayer } from './VoicePlayer';
import ImageLightbox, { type LightboxImage } from './ImageLightbox';
import MessageImageCarousel from './MessageImageCarousel';
import ChatMarkdown from './ChatMarkdown';
import { splitMessageBubbles } from '../lib/messageBubbles';

import GameEventCard from './GameEventCard';
import WorkshopEventCard from './WorkshopEventCard';
import WorkshopGiftCard from '../workshop/WorkshopGiftCard';
import { invitationDisplay } from '../workshop/invitationDisplay';









import BandEventNotice from './BandEventNotice';
import MemoryRecallInspector from './MemoryRecallInspector';
import MusicLibraryEventCard from './MusicLibraryEventCard';
import XhsPostCard from './XhsPostCard';

import CheckupExperienceDetails from './CheckupExperienceDetails';
import { hiddenBandReceipt, receiptOnlyRow } from '../lib/chatReceipts';
import { parsePersistedImageMessage } from '../lib/imageMessage';
import '../pages/LoungePage.css';

function AsyncAttachment({ toolCall }: { toolCall: ToolCallRecord; sessionId: string; messageId: string; onOpen: (src: string) => void }) {
  const ed = toolCall.extra_data || {};
  const status = ed.attachment_status || (ed.voice_pending ? 'pending' : ed.voice_path ? 'ready' : 'failed');  
  if (toolCall.tool === 'send_voice' && status !== 'ready') {
    const executionStatus = toolCall.status || (toolCall.success === false ? 'failure' : '');
    const wasNotExecuted = ['rejected', 'skipped', 'blocked'].includes(executionStatus);
    if (wasNotExecuted) {
      const reason = toolCall.error || toolCall.result || '参数不完整';
      return <div className="async-voice rejected"><span>语音未执行：{reason}</span></div>;
    }
    return <div className={`async-voice ${status}`}>
      {status === 'pending' && <span className="async-spinner">◌</span>}
      <span>{status === 'pending' ? 'AI 正在生成语音…' : (ed.attachment_error || toolCall.error || '语音生成失败')}</span>
    </div>;
  }
  return null;
}

function XhsCommentDelivery({ toolCall, onOpen }: { toolCall: ToolCallRecord; onOpen: (src: string) => void }) {
  const ed = toolCall.extra_data || {};
  if (!ed.xhs_comment || !Array.isArray(ed.images) || ed.images.length === 0) return null;
  const image = ed.images[0] || {};
  const url = typeof image.url === 'string' ? image.url : '';
  // The backend ownership boundary only emits this local static prefix.  Do
  // not let a forged tool record turn the chat into a remote/data URL loader.
  if (!url.startsWith('/static/chat_images/')) return null;
  const src = `${getApiBase()}${url}`;
  const draft = typeof ed.comment_draft === 'string' ? ed.comment_draft : '';
  return (
    <div className="xhs-comment-delivery">
      <button type="button" className="xhs-comment-image" onClick={() => onOpen(src)} title="查看帖子首页截图">
        <img src={src} alt="小红书帖子首页截图" />
      </button>
      <div className="xhs-comment-draft"><span>拟发评论：</span>{draft || '（未提供评论草稿）'}</div>
      <div className="xhs-comment-handoff">请人类伙伴查看截图后手动发送；AI 未执行评论操作。</div>
    </div>
  );
}

// ── 简单 hash（TTS 缓存键）──────────────────────────────────
function simpleHash(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h) + s.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h).toString(16);
}

/** 从用户消息内容提取 [图片消息]（视觉描述）中的描述（供图片底下折叠展示）。 */
function extractImageDescription(content: string): string | null {
  return parsePersistedImageMessage(content).description;
}

/** 描述展示用：去掉 \n / \n\n 换行符，压缩多余空白 */
function compactDescription(desc: string): string {
  return desc.replace(/[\r\n]+/g, ' ').replace(/\s{2,}/g, ' ').trim();
}

// ── TTS 全局锁（防止多个消息同时 auto-play）─────
let _ttsInFlight = false;

// ── TTS 播放按钮 ─────────────────────────────────

function TtsPlayButton({ content }: { content: string }) {
  const [ttsState, setTtsState] = useState<'idle' | 'loading' | 'playing'>('idle');
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const autoTriggered = useRef(false);

  const doTts = useCallback(async () => {
    if (ttsState === 'loading' || _ttsInFlight) return;
    if (ttsState === 'playing' && audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = '';
      audioRef.current = null;
      setTtsState('idle');
      return;
    }

    _ttsInFlight = true;
    setTtsState('loading');
    try {
      const contentHash = simpleHash(content);
      const apiBase = getApiBase();
      const resp = await fetch(`${apiBase}/api/voice/tts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: content, content_hash: contentHash }),
      });
      if (!resp.ok) throw new Error(`TTS failed: ${resp.status}`);
      const data = await resp.json();

      let audioUrl: string;
      if (data.voice_path) {
        audioUrl = data.voice_path.startsWith('/')
          ? `${apiBase}${data.voice_path}`
          : data.voice_path;
      } else if (data.audio) {
        audioUrl = `data:audio/wav;base64,${data.audio}`;
      } else {
        throw new Error('No audio in response');
      }

      const audio = new Audio(audioUrl);
      audioRef.current = audio;
      audio.onended = () => { setTtsState('idle'); audioRef.current = null; _ttsInFlight = false; };
      audio.onerror = () => { setTtsState('idle'); audioRef.current = null; _ttsInFlight = false; };
      await audio.play();
      setTtsState('playing');
      _ttsInFlight = false;
    } catch (e) {
      console.warn('TTS play failed:', e);
      setTtsState('idle');
      audioRef.current = null;
      _ttsInFlight = false;
    }
  }, [content, ttsState]);

  // 自动播报：仅 mount 时触发一次，且页面可见 + 无其他 TTS 在飞
  useEffect(() => {
    if (autoTriggered.current || _ttsInFlight || document.hidden) return;
    try {
      if (localStorage.getItem('autoPlayVoice') === 'true') {
        autoTriggered.current = true;
        doTts();
      }
    } catch {}
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleTts = useCallback(async () => {
    autoTriggered.current = true; // 手动点击也标记为已触发
    await doTts();
  }, [doTts]);

  const icon = ttsState === 'loading' ? '⏳' : ttsState === 'playing' ? '⏸' : '🔊';
  const title = ttsState === 'loading' ? '合成中...' : ttsState === 'playing' ? '暂停' : '朗读';

  return (
    <button
      className={`tts-play-btn ${ttsState}`}
      onClick={handleTts}
      title={title}
      aria-label={title}
    >
      {icon}
    </button>
  );
}

// ── 工具调用展示组件（可展开查看结果）──────────────────────────

const TOOL_ICONS: Record<string, string> = {
  '共享歌曲': '🎵',
  capture_camera: '📷',
  capture_screenshot: '🖥️',
  check_phone: '📱',
  mobile_screenshot: '📱',
  mobile_notifications: '🔔',
  band: '⌚',
  mobile_camera: '📷',
  mobile_steps_sensor: '👣',
  mobile_heart_rate: '❤️',
  mobile_ble_scan: '🟦',
  mobile_active_duration: '⏱️',
  mobile_battery: '🔋',
  mobile_usage: '📊',
  mobile_notify: '🔔',
  mobile_wifi_status: '📶',
  bookshelf: '📚',  fetch_source_details: '🔎',
  keep_in_mind: '🧠',
  manage_ledger: '📒',
  manage_scheduled_task: '⏰',
  get_weather: '🌤️',
  web_search: '🔍',
  cloud_music: '🎵',
  toys: '💗',
  toy2: '💗',
  send_voice: '🔊',  third_state_capability: '🍠',
  browse_xiaohongshu: '🍠',
  take_screenshot: '📱',
  eyes: '👀',
  web_browser: '🌐',
  manage_calendar: '📅',
  create_scheduled_task: '⏰',
  schedule_self_task: '🗓️',
  // 漫想事件卡片（manager.py _extract_tool_calls_from_event 的中文 tool 名）
  '屏幕截取': '📱',
  'GLM-4V场景分析': '👀',
  '关键词联想': '💡',
  '关键词扩写': '💡',
  '随笔': '✍️',
  '记忆检索': '🧠',
  '回看记忆': '🧠',
  '网上搜索': '📰',
  '看新闻': '📰',
  '自我审视': '🪞',
  '自省': '🪞',
  '翻看收藏夹': '⭐',
  '翻收藏夹': '⭐',
  '逛书柜': '📚',
  '刷小红书': '🍠',
  '自己听歌': '🎧',
  '听歌': '🎧',
  '玩游戏': '🎮',
  '休眠': '🌙',
  '查岗': '👁️',
  '用户追踪': '👁️',
  '小红书评论交接': '🍠',
};

export function getToolIcon(toolName: string): string {
  const normalized = String(toolName || '').trim().replace(/^[^\p{L}\p{N}_]+/u, '').trim();
  return TOOL_ICONS[toolName] || TOOL_ICONS[normalized] || '🧰';
}

const TOOL_DISPLAY_NAMES: Record<string, string> = {
  third_state_capability: '浏览小红书',
  browse_xiaohongshu: '浏览小红书',
  take_screenshot: '查看屏幕',
  capture_screenshot: '查看屏幕',
  capture_camera: '查看相机',
  web_search: '搜索网页',
  web_browser: '浏览网页',
  manage_ledger: '使用记账本',
  manage_calendar: '查看日历',
  manage_scheduled_task: '管理提醒',
  create_scheduled_task: '创建提醒',
  schedule_self_task: '安排自主任务',  cloud_music: '操作音乐',
  send_voice: '发送语音',  eyes: '视觉感知',
};

function getToolDescription(toolCall: ToolCallRecord): string {
  const toolName = String(toolCall.tool || '').trim();
  const description = String(toolCall.description || '').trim();
  const fallback = TOOL_DISPLAY_NAMES[toolName] || toolName || '使用工具';
  if (!description || description === toolName) return fallback;
  return description
    .replace(/third_state_capability/gi, '浏览小红书')
    .replace(/^(AI\s*){2,}(?=[\u3400-\u9fff])/i, 'AI');
}

const SOCIAL_FEED_TOOL_NAMES = new Set([
  'manage_social_feed', '💌 打开了朋友圈', '💭 逛朋友圈', '逛朋友圈', '打开了朋友圈', '打开了共域',
]);

const isSocialFeedToolCall = (toolCall: ToolCallRecord): boolean =>
  SOCIAL_FEED_TOOL_NAMES.has(String(toolCall.tool || '').trim());

type ToolImageOpener = (src: string, filename?: string, alt?: string) => void;
type EyesAnalysis = { summary?: unknown; visible_evidence?: unknown; confidence?: unknown };
type EyesSource = {
  source?: unknown;
  label?: unknown;
  confidence?: unknown;
  analysis?: EyesAnalysis;
  attachment?: { url?: unknown; filename?: unknown };
};

function safeSensoryImage(attachment: EyesSource['attachment']): { src: string; filename: string } | null {
  if (!attachment || typeof attachment.url !== 'string') return null;
  if (!attachment.url.startsWith('/static/sensory_captures/')) return null;
  const filename = typeof attachment.filename === 'string' ? attachment.filename : '感知快照';
  return { src: `${getApiBase()}${attachment.url}`, filename };
}

function EyesToolDetails({ toolCall, onOpenImage }: { toolCall: ToolCallRecord; onOpenImage: ToolImageOpener }) {
  const extra = toolCall.extra_data || {};
  const sources: EyesSource[] = Array.isArray(extra.sources)
    ? extra.sources.filter((source: unknown): source is EyesSource => Boolean(source) && typeof source === 'object')
    : [];
  const errors = Array.isArray(extra.errors) ? extra.errors.filter((item: unknown) => typeof item === 'string') : [];

  return (
    <div className="eyes-tool-details">
      {toolCall.result && <div className="eyes-tool-summary">{toolCall.result}</div>}
      <div className="eyes-tool-source-grid">
        {sources.map((source, index) => {
          const analysis = source?.analysis && typeof source.analysis === 'object' ? source.analysis : {};
          const image = safeSensoryImage(source?.attachment);
          const label = String(source?.label || source?.source || `来源 ${index + 1}`);
          const summary = String(analysis.summary || '').trim();
          const evidence = String(analysis.visible_evidence || '').trim();
          const confidence = analysis.confidence ?? source?.confidence;
          return (
            <section className="eyes-tool-source" key={String(source?.source || index)}>
              <div className="eyes-tool-source-head">
                <strong>{label}</strong>
                {confidence !== undefined && confidence !== '' && confidence !== 'pending' && (
                  <span className="eyes-tool-confidence">可信度 {String(confidence)}</span>
                )}
              </div>
              {image ? (
                <button type="button" className="eyes-tool-image" onClick={() => onOpenImage(image.src, image.filename, `${label}原始画面`)}>
                  <img src={image.src} alt={`${label}原始画面`} loading="lazy" />
                  <span>查看原图</span>
                </button>
              ) : <div className="eyes-tool-image-missing">本次没有可保留的原图</div>}
              <div className="eyes-tool-observation">
                {summary || evidence ? (
                  <>
                    {summary && <p>{summary}</p>}
                    {evidence && evidence !== summary && <small>画面依据：{evidence}</small>}
                  </>
                ) : <p className="eyes-tool-pending">未取得这一来源的视觉分析</p>}
              </div>
            </section>
          );
        })}
      </div>
      {sources.length === 0 && <div className="eyes-tool-empty">没有取得摄像头来源。</div>}
      {errors.length > 0 && <div className="eyes-tool-errors">未取得：{errors.join('；')}</div>}
      {toolCall.error && <div className="eyes-tool-errors">分析异常：{toolCall.error}</div>}
    </div>
  );
}



function ToolCallsDisplay({ toolCalls, onOpenImage, activityEvent = false }: { toolCalls: ToolCallRecord[]; onOpenImage: ToolImageOpener; activityEvent?: boolean; onOpenBook?: () => void }) {
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  useEffect(() => {
    if (expandedIdx === null) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setExpandedIdx(null);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [expandedIdx]);
  if (!toolCalls || toolCalls.length === 0 || toolCalls.every(hiddenBandReceipt)) return null;

  const expandedEyes = expandedIdx === null ? null : toolCalls[expandedIdx]?.tool === 'eyes' ? toolCalls[expandedIdx] : null;

  return (
    <div className="tool-calls-record">
      {toolCalls.map((tc, idx) => (
        tc.tool === 'band_touch' ? <BandEventNotice key={idx} call={tc} /> : isSocialFeedToolCall(tc) && !activityEvent ? (
          // A social-feed operation is a one-shot UI affordance.  Older
          // persisted records may contain an action/result, but those fields
          // are deliberately never rendered as chat-visible tool details.
          <div key={idx} className="tool-call-item-wrapper social-feed-tool-wrapper">
            <div className="tool-call-item social-feed-tool-item" role="status" aria-label="打开了共域">
              <span className="tool-call-icon" aria-hidden="true">💌</span>
              <span className="tool-call-desc">打开了共域</span>
            </div>
          </div>
        ) : (
          <div key={idx} className="tool-call-item-wrapper">
            <div
              className="tool-call-item"
              onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
              style={{ cursor: 'pointer' }}
            >
              <span className="tool-call-expand">{expandedIdx === idx ? '▼' : '▶'}</span>
              <span className="tool-call-icon">{getToolIcon(tc.tool)}</span>
              <span className="tool-call-desc">{getToolDescription(tc)}</span>
              {(tc.result || tc.error) && (
                <span className={`tool-call-result tool-call-result-${tc.status || 'success'}`} title={tc.error || tc.status}>
                  {tc.status === 'failure' || tc.status === 'rejected'
                    ? '×'
                    : tc.status === 'partial'
                      ? '◐'
                      : tc.status === 'skipped'
                        ? '—'
                        : '✓'}
                </span>
              )}
            </div>
            {expandedIdx === idx && tc.tool !== 'eyes' && tc.result && (
              Array.isArray(tc.extra_data?.memory_experience?.materials)
                && tc.extra_data.memory_experience.materials.some((item: { kind?: string }) => item.kind === 'checkup')
                ? <CheckupExperienceDetails toolCall={tc} /> :
              <div className="tool-call-result-content">
                <pre style={{
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  margin: 0,
                  fontSize: '0.85em',
                  maxHeight: '400px',
                  overflowY: 'auto',
                  background: 'rgba(0,0,0,0.05)',
                  padding: '8px 12px',
                  borderRadius: '6px',
                }}>
                  {tc.result}
                </pre>
              </div>
            )}
          </div>
        )
      ))}
      {expandedEyes && createPortal(
        <div className="eyes-tool-modal-backdrop" role="presentation" onClick={() => setExpandedIdx(null)}>
          <div className="eyes-tool-modal" role="dialog" aria-modal="true" aria-label="AI 看人类伙伴的感知详情" onClick={(event) => event.stopPropagation()}>
            <div className="eyes-tool-modal-head">
              <div><span aria-hidden="true">👀</span> AI 看人类伙伴</div>
              <button type="button" onClick={() => setExpandedIdx(null)} aria-label="关闭">×</button>
            </div>
            <EyesToolDetails toolCall={expandedEyes} onOpenImage={onOpenImage} />
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

// ── 消息编辑区组件 ────────────────────────────────────────────



// ── 单条消息行组件 ────────────────────────────────────────────

export interface MessageRowProps {
  message: Message;
  onChooseReply?: (text: string) => Promise<void> | false;
  replyChoicesDisabled?: boolean;
  onOpenBook?: () => void;
  userAvatar?: string;
  personaAvatar?: string;
  personaName?: string;
  isEditing?: boolean;
  isThinkingExpanded?: boolean;
  isSummarized?: boolean;
  formatTimestamp?: (date: Date) => string;
  onToggleThinking?: () => void;
  onStartEdit?: () => void;
  onDelete?: () => void;
  onRevoke?: () => void;
  readOnly?: boolean;
  isBookmarked?: boolean;
  onBookmark?: () => void;
  onSaveEdit?: (messageId: string, content: string) => void;
  onResendEdit?: (messageId: string, content: string, replacementId: string) => void | Promise<void>;
  editDraft?: MessageEditDraft;
  onCancelEdit?: () => void;
  /** 当前消息正在执行撤回并重发；用于阻止按钮双击制造两个新轮次。 */
  isResending?: boolean;
  currentSessionId?: string;
  /** 搜索定位命中：整条消息金色闪烁 2.5s（纯 className 切换，不换 key） */
  isHighlighted?: boolean;
  /** 隐藏收藏按钮（后宫等不需要书签功能的场景） */
  hideBookmark?: boolean;
  /** 主聊天 AI 消息旁的本地记忆诊断；群聊等独立域关闭。 */
  hideMemoryInspector?: boolean;
  /** 打开全局许愿板弹窗（notification 与漫想日志入口共用） */
  onOpenWishBoard?: (metadata?: Record<string, any>) => void;
  /** 打开朋友圈（social_feed_update 通知入口） */
  onOpenSocialFeed?: () => void;
  
  onOpenWorkshop?: (jobId: string) => void;
  /** 人类伙伴消息头像下方的快捷心情上报入口；绝对定位，不参与消息行测高 */
  onOpenMoodCheckin?: () => void;
  onLongTextCardUpdated?: (messageId: string, card: NonNullable<Message['longTextCard']>) => void;
}

// ── 消息反馈按钮（赞/踩）─────────────────────────────────

function MessageFeedback({ message, currentSessionId }: { message: Message; currentSessionId: string }) {
  const [feedback, setFeedback] = useState<'like' | 'dislike' | null>(null);
  const [showReason, setShowReason] = useState(false);
  const [reason, setReason] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const submitFeedback = async (type: 'like' | 'dislike', reasonText: string) => {
    setFeedback(type);
    setSubmitted(true);
    setShowReason(false);
    try {
      await fetch(`${getApiBase()}/api/messages/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: currentSessionId,
          message_id: message.id,
          feedback_type: type,
          reason: reasonText,
        }),
      });
    } catch (e) {
      console.error('反馈提交失败:', e);
      setFeedback(null);
      setSubmitted(false);
    }
  };

  const handleClick = (type: 'like' | 'dislike') => {
    if (feedback === type) {
      // 取消已选的反馈
      setFeedback(null);
      setSubmitted(false);
      return;
    }
    if (type === 'dislike') {
      // 踩 → 弹出原因输入框
      setShowReason(true);
      return;
    }
    // 赞 → 直接提交
    submitFeedback('like', '');
  };

  const handleReasonSubmit = () => {
    submitFeedback('dislike', reason.trim());
    setReason('');
  };

  return (
    <div className={`message-feedback${showReason ? ' reason-open' : ''}`}>
      <button
        type="button"
        className={`message-feedback-btn${feedback === 'like' ? ' active' : ''}`}
        onClick={() => handleClick('like')}
        title={feedback === 'like' ? '已赞（点击取消）' : '赞'}
      >❤️</button>
      <button
        type="button"
        className={`message-feedback-btn${feedback === 'dislike' ? ' active' : ''}`}
        onClick={() => handleClick('dislike')}
        title={feedback === 'dislike' ? '已踩（点击取消）' : '踩'}
      >💔</button>
      {submitted && feedback === 'like' && (
        <span className="message-feedback-status">已赞</span>
      )}
      {showReason && (
        <div className="message-feedback-reason">
          <input
            autoFocus
            value={reason}
            onChange={e => setReason(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleReasonSubmit(); if (e.key === 'Escape') setShowReason(false); }}
            placeholder="为什么不喜欢？"
          />
          <button type="button" onClick={handleReasonSubmit}>发送</button>
          <button type="button" onClick={() => setShowReason(false)}>×</button>
        </div>
      )}
    </div>
  );
}

export default function MessageRow({
  message,  userAvatar = '', personaAvatar = '🤖', personaName = 'AI',
  isEditing = false, isThinkingExpanded = false, isSummarized = false,
  formatTimestamp = date => date.toLocaleString(),
  onToggleThinking = () => {}, onStartEdit = () => {}, onDelete = () => {}, onRevoke = () => {},
  isBookmarked = false, onBookmark = () => {}, onSaveEdit = () => {}, onResendEdit = () => {}, onCancelEdit = () => {}, editDraft,
  isResending = false,
  currentSessionId = '',
  onOpenWishBoard = () => {},
  onOpenSocialFeed = () => {},  onOpenWorkshop = () => {},
  onOpenMoodCheckin,
  onLongTextCardUpdated = () => {},
  isHighlighted,
  hideBookmark = false,
  hideMemoryInspector = false,
  readOnly = false,
}: MessageRowProps) {
  // 思考步骤平滑收起：实测内容高度 → CSS transition，避免 DOM 突变导致 Virtuoso 跳动
  const stepsInnerRef = useRef<HTMLDivElement>(null);
  const [thinkingContentHeight, setThinkingContentHeight] = useState(0);
  // 旧消息和部分实时消息可能没有 thinkingChain。思考链入口仍属于
  // assistant 消息的稳定 UI，让「0 步」也能被展开查看。空态提示只用于
  // 展示，不写入 thinkingChain，也不把提示或正文伪装成思考步骤。
  const thinkingSteps = Array.isArray(message.thinkingChain) ? message.thinkingChain : [];

  useEffect(() => {
    if (isThinkingExpanded && stepsInnerRef.current) {
      const h = stepsInnerRef.current.scrollHeight;
      if (h > 0 && h !== thinkingContentHeight) setThinkingContentHeight(h);
    }
  }, [isThinkingExpanded, message.thinkingChain]);

  // 图片 lightbox
  const [lightbox, setLightbox] = useState<{src: string; filename?: string; alt?: string; images?: LightboxImage[]; initialIndex?: number} | null>(null);
  const openLightbox = useCallback((src: string, filename?: string, alt?: string) => {
    setLightbox({ src, filename, alt });
  }, []);
  const openImageCollection = useCallback((images: LightboxImage[], index: number) => {
    setLightbox({ ...images[index], images, initialIndex: index });
  }, []);

  // 图片内容 GLM 描述折叠（人类伙伴 2026-08-22：不是用户消息，挂在图片底下可展开）
  const _imageDesc = message.role === 'user' ? extractImageDescription(message.content) : null;
  const [imgDescExpanded, setImgDescExpanded] = useState(false);

  // 群聊摘要展开状态
  
  const [checkupExpanded, setCheckupExpanded] = useState(false);
  const [memoryRecallOpen, setMemoryRecallOpen] = useState(false);
  const [longCardOpen, setLongCardOpen] = useState(false);
  const [localLongCard, setLocalLongCard] = useState(message.longTextCard);
  const [editingLongCard, setEditingLongCard] = useState(false);
  const [longCardDraft, setLongCardDraft] = useState({ title: '', body: '' });
  useEffect(() => { setLocalLongCard(message.longTextCard); }, [message.longTextCard]);
  useEffect(() => {
    if (!checkupExpanded) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCheckupExpanded(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [checkupExpanded]);

  // Mobile WebView does not consistently focus a tabbable div after a touch.
  // Keep the selected-message affordance explicit and close it when the next
  // pointer interaction happens outside this message.
  const messageRef = useRef<HTMLDivElement>(null);
  const [actionsOpen, setActionsOpen] = useState(false);
  useEffect(() => {
    if (!actionsOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!messageRef.current?.contains(event.target as Node)) setActionsOpen(false);
    };
    document.addEventListener('pointerdown', closeOutside, true);
    return () => document.removeEventListener('pointerdown', closeOutside, true);
  }, [actionsOpen]);

  // 摘要标记可能来自 WS，也可能来自 SQLite 重载后的 eventType。正式
  // 摘要会另外写一条固定 divider；它保持 divider 样式，只有实际摘要正文
  // 渲染为可展开卡片，避免同一摘要出现两张卡。
  const eventType = (message as any).eventType || (message as any).event_type;
  
  if (eventType === 'workshop_event') {
    const card = message.toolCalls?.find(call => call.extra_data?.workshop)?.extra_data?.workshop;
    if (card) return <WorkshopEventCard card={card} onOpen={onOpenWorkshop} />;
  }
  if (eventType === 'social_gift') {
    return <div className="lounge-event-row"><details className="social-gift-event"><summary className="lounge-event-trigger"><span className="lounge-event-icon" aria-hidden="true">🎁</span><span className="lounge-event-copy">共域赠礼 · 点击查看</span></summary><p>{message.content}</p></details></div>;
  }
  if (eventType === 'wander_activity') {
    const calls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
    return <div key={message.id} className="message notification tool-event-row">
      <ToolCallsDisplay toolCalls={calls} onOpenImage={openLightbox} activityEvent  />
    </div>;
  }
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  

  // 群聊摘要卡片必须先于 system 分支判断；SQLite 重载的消息 role 是
  // system，若顺序反过来会被当成普通小字系统提示而无法展开。
  

  // 分隔线
  if (message.role === 'divider') {
    return (
      <div key={message.id} className="message divider">
        <div className="divider-line">
          <span className="divider-text">{message.content}</span>
        </div>
      </div>
    );
  }

  // system 消息：无气泡、小字居中（群聊摘要 divider + AI 旁白）
  if (message.role === 'system') {
    return (
      <div key={message.id} className="message system-note">
        <div className="system-note-text">{message.content}</div>
      </div>
    );
  }

  // 事件行与 UI 通知共用无气泡外壳；工具事件由原消息回执派生，
  // 普通通知仍按各自 eventType 的后端投影规则进入或不进入上下文。
  if (message.role === 'notification' || message.role === 'event') {
    // SQLite/older WS payloads may expose snake_case; normalize here too so
    // a social_feed_update always keeps its feed-opening affordance.
    const notificationEventType = message.eventType || (message as any).event_type;
    if (notificationEventType === 'music_library_event') {
      return <MusicLibraryEventCard key={message.id} content={message.content} />;
    }
    if (notificationEventType === 'wander_activity') {
      const gameCall = (message.toolCalls || []).find(call => call.tool === '🎮 玩游戏');
      if (gameCall) return <GameEventCard key={message.id} eventId={message.id} summary={message.content} call={gameCall} />;
    }
    if (notificationEventType === 'tool_event') {
      const calls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
      return <div key={message.id} className="message notification tool-event-row">
        <ToolCallsDisplay toolCalls={calls} onOpenImage={openLightbox}  />
      </div>;
    }
    if (notificationEventType === 'band_action') {
      const calls = message.toolCalls || (message as any).tool_calls;
      const items = typeof calls === 'string' ? (() => { try { return JSON.parse(calls); } catch { return []; } })() : calls;
      return <div className="band-notification-row">{Array.isArray(items) && items.length
        ? items.map((call: ToolCallRecord, index: number) => <BandEventNotice key={index} call={call} independent />)
        : <span className="band-event-notice">{message.content}</span>}</div>;
    }
    const notificationMetadata = (message as any).notificationMetadata
      || (message as any).notification_metadata
      || (message as any).metadata;
    const isSocialFeedUpdate = notificationEventType === 'social_feed_update';
    const isCheckup = notificationEventType === 'checkup_observation';
    const isAmbientObservation = notificationEventType === 'ambient_listening_observation';
    const checkupContent = message.content.replace(/^[👀👁]\uFE0F?\s*/u, '').trim();
    const checkupItems = Array.isArray(message.toolCalls) ? message.toolCalls : [];
    const notificationAction = notificationEventType === 'wish_board_update'
      ? onOpenWishBoard
      : isSocialFeedUpdate
        ? onOpenSocialFeed
        : undefined;
    const clickable = Boolean(notificationAction);
    if (isCheckup) {
      const statusLabel = (status?: string) => ({
        success: '成功', failure: '失败', partial: '部分结果', skipped: '未执行', executing: '执行中',
      }[status || 'partial'] || status || '部分结果');
      return (
        <div key={message.id} className="message notification checkup-observation">
          <button type="button" className="notification-line checkup-observation-summary" onClick={() => setCheckupExpanded(true)}>
            👁️ {checkupContent}<span className="checkup-open-hint">查看详情</span>
          </button>
          {checkupExpanded && createPortal(
            <div className="checkup-modal-backdrop" role="presentation" onMouseDown={() => setCheckupExpanded(false)}>
              <section className="checkup-modal" role="dialog" aria-modal="true" aria-label="查岗详情" onMouseDown={event => event.stopPropagation()}>
                <header className="checkup-modal-header">
                  <div><h3>👁️ 查岗详情</h3><p>{checkupContent}</p></div>
                  <button type="button" className="checkup-modal-close" aria-label="关闭" onClick={() => setCheckupExpanded(false)}>×</button>
                </header>
                <div className="checkup-observation-details">
                  {checkupItems.length ? checkupItems.map((item, index) => (
                    <div className="checkup-observation-item" key={`${item.tool}-${index}`}>
                      <div className="checkup-item-heading">
                        <span className="checkup-tool-name">{item.description || item.tool}</span>
                        <span className={`checkup-status checkup-status-${item.status || 'partial'}`}>{statusLabel(item.status)}</span>
                      </div>
                      <div className="checkup-tool-result">{item.result || item.error || '没有取得足够信息'}</div>
                    </div>
                  )) : <div className="checkup-observation-empty">这次没有取得可展示的感知结果。</div>}
                </div>
              </section>
            </div>
          , document.body)}
        </div>
      );
    }
    if (isAmbientObservation) {
      return <div key={message.id} className="message notification ambient-observation">
        <details className="ambient-observation-details">
          <summary>🎙️ AI记录了一段环境声音 <span>查看观察</span></summary>
          <div className="ambient-observation-body">
            <p>{message.content}</p>
            <small>这是当时的观察摘要；说话人和设备声音来源以记录中的依据为准。</small>
          </div>
        </details>
      </div>;
    }
    return (
      <div key={message.id} className={`message notification${clickable ? ' clickable' : ''}${isSocialFeedUpdate ? ' social-feed-update' : ''}`} onClick={() => notificationAction?.(notificationMetadata)}
        role={clickable ? 'button' : undefined} tabIndex={clickable ? 0 : undefined}
        onKeyDown={clickable ? (e => { if (e.key === 'Enter' || e.key === ' ') notificationAction?.(notificationMetadata); }) : undefined}>
        <span className={`notification-line${isSocialFeedUpdate ? ' social-feed-update-line' : ''}`}>
          {isSocialFeedUpdate ? message.content.replaceAll('朋友圈', '共域动态') : message.content}
        </span>
      </div>
    );
  }

  const displayContent = message.role === 'user' && eventType === 'ui_action:visit_workshop'
    ? invitationDisplay(message.content) : message.content;
  const segments = splitMessageBubbles(displayContent, message.role);
  
  const workshopGifts = message.role === 'assistant'
    ? (message.toolCalls || []).filter(tc => tc.extra_data?.workshop?.kind === 'artifact_shared' && tc.extra_data?.workshop?.source === 'k' && tc.extra_data?.workshop?.artifacts?.length)
    : [];
  const supportingShare = workshopGifts.length > 0 && workshopGifts.every(tc => tc.extra_data!.workshop.artifacts.every((item:{name:string}) => !/\.(png|jpe?g|webp|gif|html?|txt|md)$/i.test(item.name)));
  const ordinaryToolCalls = message.role === 'assistant'
    ? (message.toolCalls || []).filter(tc => !workshopGifts.includes(tc) && tc.tool !== 'send_voice' && tc.tool !== 'ambient_event_link' && tc.extra_data?.xhs_comment !== true && !tc.extra_data?.lounge_visit && !tc.extra_data?.music_card && !tc.extra_data?.xhs_post_card && !tc.extra_data?.essay_card && !tc.extra_data?.reading_illustration && tc.extra_data?.attachment_kind !== 'music')
    : [];

  // A receipt with no prose/media/reasoning is not an empty chat message.
  // Keep its stored record intact; only suppress the chat chrome.
  if (!isEditing && receiptOnlyRow(message)) {
    const visible = (message.toolCalls || []).filter(call => !hiddenBandReceipt(call));
    return visible.length ? <div className="band-notification-row">{visible.map((call,index) => <BandEventNotice key={index} call={call}/>)}</div> : null;
  }

  const renderMessageActionButtons = () => !readOnly && (message.role === 'user' || message.role === 'assistant') && (
    <>
      {!['ui_action:visit_lounge', 'ui_action:play_game', 'ui_action:visit_workshop'].includes(eventType || '') && <button className="message-action-btn edit-btn" onClick={onStartEdit} title="编辑消息">✎</button>}
      {!hideBookmark && (
        <button
          className={`message-action-btn bookmark-btn ${isBookmarked ? 'bookmarked' : ''}`}
          onClick={onBookmark}
          title={isBookmarked ? '取消收藏' : '收藏消息'}
        >
          {isBookmarked ? '⭐' : '☆'}
        </button>
      )}
      {message.role === 'assistant' && !hideMemoryInspector && (
        <button
          className="message-action-btn memory-btn"
          onClick={() => setMemoryRecallOpen(true)}
          title="查看这条回复的记忆召回"
          aria-label="查看这条回复的记忆召回"
        >记</button>
      )}
      <button className="message-action-btn delete-btn" onClick={onDelete} title="删除消息">×</button>
      {message.role === 'user' && (
        <button
          className="message-action-btn revoke-btn"
          onClick={onRevoke}
          title="撤回消息（删除此消息及之后的所有消息）"
        >
          ↶
        </button>
      )}
    </>
  );

  // ── 统一多气泡布局（1段=1气泡，多段=多气泡，结构一致）─────────

  return (
    <>
    {memoryRecallOpen && (
      <MemoryRecallInspector
        assistantMessageId={message.id}
        onClose={() => setMemoryRecallOpen(false)}
      />
    )}
    <div
      key={message.id}
      ref={messageRef}
      className={`message ${message.role}${isHighlighted ? ' locate-highlight' : ''}${actionsOpen ? ' actions-open' : ''}`}
      tabIndex={0}
      onClick={(event) => {
        const target = event.target as HTMLElement;
        if (!target.closest('.message-bubble, .message-avatar, .message-header')) return;
        setActionsOpen(true);
        event.currentTarget.focus({ preventScroll: true });
      }}
    >
      <div className="message-avatar-anchor">
        <div className="message-avatar">
          {message.role === 'user'
            ? (userAvatar.startsWith('data:')
              ? <img src={userAvatar} alt="用户头像" className="avatar-img" />
              : userAvatar)
            : personaAvatar.startsWith('data:')
              ? <img src={personaAvatar} alt="AI头像" className="avatar-img" />
              : personaAvatar}
        </div>
      </div>
      <div className="message-group-content">
        <div className="message-header">
          <span className="message-role">
            {message.role === 'user' ? '人类伙伴' : message.isSentinel ? `AI🛡️` : message.isWander ? `AI💭` : message.isReminder ? `AI⏰` : (message as any).isMusicCochlea ? `AI🎵` : personaName}
          </span>
          <span
            className={`message-summary-indicator ${isSummarized ? 'summarized' : 'unsummarized'}`}
            title={isSummarized ? '已总结' : '未总结'}
          >
            ●
          </span>
          <span className="message-time">
            {formatTimestamp(message.timestamp)}
          </span>
          {/* TTS 播放按钮：仅 AI 的消息，放在 header 行不占空间 */}
          {message.role === 'assistant' && message.content && message.content.trim() && (
            <TtsPlayButton content={message.content} />
          )}
          <div className="message-actions">
            {renderMessageActionButtons()}
          </div>
        </div>

        {message.role === 'user' && ['ui_action:visit_lounge', 'ui_action:play_game', 'ui_action:visit_workshop'].includes(eventType || '') && (
          <small className="quick-action-source" title={eventType === 'ui_action:visit_workshop' ? '由人类伙伴点击按钮发起，只显示短邀请与实际附话' : '由人类伙伴点击按钮发起，正文为界面固定文案'}>快捷操作 · {eventType === 'ui_action:visit_workshop' ? '邀请来工作坊' : eventType === 'ui_action:visit_lounge' ? '拜访会客室' : eventType === 'ui_action:play_game' ? '邀请玩游戏' : '快捷邀请'}</small>
        )}
        {isEditing ? (
          <MessageEditArea
            initialContent={message.content}
            draft={editDraft}
            message={message}
            isLoading={isResending}
            onSave={onSaveEdit}
            onResend={onResendEdit}
            onCancel={onCancelEdit}
          />
        ) : (
          <div className="bubble-group">
            {message.role === 'user' && onOpenMoodCheckin && (
              <button
                type="button"
                className="message-avatar-mood-button"
                onClick={onOpenMoodCheckin}
                title="快速报告心情"
                aria-label="快速报告心情"
              >
                <span aria-hidden="true">♥</span><small aria-hidden="true">＋</small>
              </button>
            )}
            {/* 语音消息：send_voice 工具调用 → 音频播放条 */}
            {message.role === 'assistant' && message.toolCalls && message.toolCalls.length > 0 && (() => {
              const voiceTracks = extractVoiceTracks(message.toolCalls!);
              const attachments = message.toolCalls!.filter(tc => tc.tool === 'send_voice');
              const xhsCommentDeliveries = message.toolCalls!.filter(tc => tc.extra_data?.xhs_comment === true);
              
              const musicCards = message.toolCalls!.filter(tc => tc.extra_data?.music_card || tc.extra_data?.attachment_kind === 'music');
              const xhsPostCards = message.toolCalls!.filter(tc => tc.extra_data?.xhs_post_card);
              const essayCards = message.toolCalls!.filter(tc => tc.extra_data?.essay_card);
              const readingPictures = message.toolCalls!.filter(tc => tc.extra_data?.reading_illustration);
              
              return (
                <>
                  {musicCards.map((tc, i) => tc.extra_data?.attachment_kind === 'music' ? <AsyncMusicCard key={tc.extra_data.generation_id || 'music-' + i} attachment={tc.extra_data} /> : <MusicCard key={'music-' + i} song={tc.extra_data!.music_card} />)}
                  {xhsPostCards.map((tc, i) => <XhsPostCard key={`xhs-post-${i}`} card={tc.extra_data!.xhs_post_card} messageId={message.id} sessionId={currentSessionId || ''} />)}
                  {essayCards.map((tc, i) => <EssayCard key={`essay-${i}`} card={tc.extra_data!.essay_card} />)}
                  {readingPictures.map((tc, i) => <ReadingPicture key={`reading-${i}`} picture={tc.extra_data!.reading_illustration} />)}
                  
                  
                  <VoicePlayer voiceTracks={voiceTracks} />
                  {attachments.map((tc, idx) => <AsyncAttachment key={`${tc.tool}-${tc.extra_data?.generation_id || tc.extra_data?.voice_id || idx}`} toolCall={tc} sessionId={currentSessionId || ''} messageId={message.id} onOpen={openLightbox} />)}
                  {xhsCommentDeliveries.map((tc, idx) => <XhsCommentDelivery key={`xhs-comment-${idx}`} toolCall={tc} onOpen={openLightbox} />)}
                </>
              );
            })()}
            {/* 用户语音：播放条 + 语气标签 + 转录 */}
            {message.voiceAttachment && (
              <div className="user-voice-wrap">
                {message.voiceAttachment.tone && (
                  <span className="user-voice-tone">{message.voiceAttachment.tone}</span>
                )}
                <SingleVoicePlayer
                  voicePath={message.voiceAttachment.path}
                  voiceText={message.voiceAttachment.text || message.content}
                />
              </div>
            )}
            {/* 用户图片：第一个气泡上方 */}
            {message.images && message.images.length > 0 && (
              message.images.length > 1 ? (
                <MessageImageCarousel images={message.images} onOpen={openImageCollection} />
              ) : (
              <div className="message-images">
                {message.images.map((img, idx) => {
                  // 相对路径补全 API 地址（Vite dev server / APK 不代理静态文件）
                  const imgSrc = img.url
                    ? (img.url.startsWith('/') ? `${getApiBase()}${img.url}` : img.url)
                    : '';
                  return (
                  <div key={idx} className="message-image-item">
                    {imgSrc ? (
                      <img src={imgSrc} alt={img.filename} className="message-image"
                        onClick={() => openLightbox(imgSrc, img.filename, img.filename)} />
                    ) : (
                      <div className="message-image-placeholder">
                        <span className="image-placeholder-icon">🖼️</span>
                        <span className="image-placeholder-name">{img.filename}</span>
                      </div>
                    )}
                  </div>
                );
                })}
              </div>
              )
            )}
            {/* 图片内容 GLM 描述折叠（不是用户消息——挂在图片底下，可展开查看） */}
            {_imageDesc && (
              <div className="image-desc-collapsible">
                <button
                  className={`image-desc-toggle ${imgDescExpanded ? 'expanded' : ''}`}
                  onClick={() => setImgDescExpanded(!imgDescExpanded)}
                >
                  {imgDescExpanded ? '收起' : '展开'}图片内容{imgDescExpanded ? ' ▲' : ' ▼'}
                </button>
                {imgDescExpanded && (
                  <div className="image-desc-body">
                    <span>{compactDescription(_imageDesc)}</span>
                  </div>
                )}
              </div>
            )}
            
            {localLongCard?.kind === 'music' && <MusicCard card={localLongCard} />}
            {localLongCard?.kind === 'xhs' && <XhsPostCard card={localLongCard} />}
            {localLongCard && !localLongCard.kind && (
              <button type="button" className="long-text-card-message" onClick={() => setLongCardOpen(true)}>
                <span className="long-text-card-message-top">
                  <span className="long-text-card-seal" aria-hidden="true">♡</span>
                  <span className="long-text-card-kicker">人类伙伴的长文小卡片</span>
                  <span className="long-text-card-open-hint">打开 <span aria-hidden="true">›</span></span>
                </span>
                <strong>{localLongCard.title || '无标题长文卡片'}</strong>
                <span className="long-text-card-preview">{localLongCard.body.slice(0, 120)}{localLongCard.body.length > 120 ? '…' : ''}</span>
                <small><span>{localLongCard.body.length.toLocaleString()} 字</span><span>轻触展开全文</span></small>
              </button>
            )}
            {/* 每段独立气泡（有语音附件时跳过文字，避免重复） */}
            
            
            {!message.voiceAttachment && !supportingShare && segments.map((seg, i) => (
              <div key={i} className="message-bubble message-markdown-bubble">
                <div className="message-text">
                  <ChatMarkdown content={seg} isUser={message.role === 'user'} />
                </div>
              </div>
            ))}
            {supportingShare && <details className="workshop-supporting-share"><summary>历史制作资料（不是作品）</summary><ChatMarkdown content={displayContent} isUser={message.role === 'user'}/>{workshopGifts.map((tc,index)=><WorkshopGiftCard key={`support-${index}`} card={tc.extra_data!.workshop} onOpen={onOpenWorkshop} attachment />)}</details>}
            {!supportingShare && workshopGifts.map((tc,index)=><WorkshopGiftCard key={`gift-${index}`} card={tc.extra_data!.workshop} onOpen={onOpenWorkshop} attachment onOpenImage={openLightbox} />)}
            {ordinaryToolCalls.length > 0 && (
              <ToolCallsDisplay toolCalls={ordinaryToolCalls} onOpenImage={openLightbox}  />
            )}
            
            {/* 思考过程：最后一个气泡下方 */}
            {message.role === 'assistant' && (workshopGifts.length === 0 || thinkingSteps.length > 0 || actionsOpen) && (
              <div className={`thinking-chain${thinkingSteps.length === 0 ? ' empty' : ''}`}>
                <div className="thinking-chain-toolbar">
                  {actionsOpen && message.modelSource && message.modelSource.ordinal > 1 && <small className="model-source-label">{message.modelSource.model_label || message.modelSource.model} · {message.modelSource.station_name}</small>}
                  <button
                    className={`thinking-chain-toggle ${isThinkingExpanded ? 'expanded' : ''}`}
                    onClick={onToggleThinking}
                  >
                    {isThinkingExpanded ? '收起思考过程' : '查看思考过程'} ({thinkingSteps.length}步)
                    <span className="thinking-chain-arrow">
                      {isThinkingExpanded ? '▲' : '▼'}
                    </span>
                  </button>
                  {!message.isWander && !message.isSentinel && (
                    <MessageFeedback message={message} currentSessionId={currentSessionId} />
                  )}
                </div>
                <div
                  style={{
                    maxHeight: isThinkingExpanded ? `${thinkingContentHeight}px` : '0px',
                    overflow: 'hidden',
                    transition: 'max-height 0.3s ease',
                  }}
                >
                  <div ref={stepsInnerRef} className={`thinking-steps${thinkingSteps.length === 0 ? ' empty' : ''}`}>
                    {thinkingSteps.length === 0 && (
                      <div className="thinking-empty-notice">
                        {message.isSentinel || message.modelSource
                          ? '本线路供应商未返回思考过程'
                          : '本条消息未记录思考过程'}
                      </div>
                    )}
                    {thinkingSteps.map((step, index) => {
                      const content = step.content.replace(/\*\*/g, '');
                      return (
                        <div key={index} className="thinking-step">
                          <div className="thinking-step-header">
                            <span className="thinking-step-number">步骤 {index + 1}</span>
                            <span className="thinking-step-time">
                              {formatTimestamp(new Date(step.timestamp))}
                            </span>
                          </div>
                          <div className="thinking-step-content">{content}</div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
    {lightbox && (
      <ImageLightbox
        src={lightbox.src}
        filename={lightbox.filename}
        alt={lightbox.alt}
        images={lightbox.images}
        initialIndex={lightbox.initialIndex}
        onClose={() => setLightbox(null)}
      />
    )}
    {longCardOpen && localLongCard && !localLongCard.kind && createPortal(
      <div className="long-text-card-modal-backdrop" onMouseDown={() => setLongCardOpen(false)}>
        <section className="long-text-card-modal" onMouseDown={e => e.stopPropagation()}>
          <header className="long-text-card-modal-header">
            <div>
              <span className="long-text-card-kicker">{editingLongCard ? '轻轻改一改，不会重新发送' : '人类伙伴的长文小卡片'}</span>
              <h3>{editingLongCard ? '编辑长文小卡片' : (localLongCard.title || '无标题长文卡片')}</h3>
            </div>
            <button className="long-text-card-close" type="button" aria-label="关闭" onClick={() => setLongCardOpen(false)}>×</button>
          </header>
          {editingLongCard ? (
            <>
              <label className="long-text-card-field">
                <span>卡片标题 <small>可选</small></span>
                <input value={longCardDraft.title} maxLength={500} placeholder="给这页心意起个名字" onChange={e => setLongCardDraft(d => ({...d,title:e.target.value}))}/>
              </label>
              <label className="long-text-card-field long-text-card-body-field">
                <span>卡片正文</span>
                <textarea rows={12} maxLength={100000} value={longCardDraft.body} onChange={e => setLongCardDraft(d => ({...d,body:e.target.value}))}/>
              </label>
              <footer className="long-text-card-modal-footer">
                <span className="long-text-card-count">{longCardDraft.body.length.toLocaleString()} 字</span>
                <div className="long-text-card-actions">
                  <button className="long-text-card-button secondary" type="button" onClick={() => setEditingLongCard(false)}>取消修改</button>
                  <button className="long-text-card-button primary" type="button" disabled={!longCardDraft.body.trim()} onClick={async () => { const next={...localLongCard!,title:longCardDraft.title.trim(),body:longCardDraft.body}; const r=await fetch(`${getApiBase()}/api/messages/long-text-card`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:currentSessionId,message_id:message.id,long_text_card:next})}); if(!r.ok){alert((await r.json().catch(()=>({}))).detail||'保存失败');return;} setLocalLongCard(next);onLongTextCardUpdated(message.id,next);setEditingLongCard(false);}}>保存修改</button>
                </div>
              </footer>
            </>
          ) : (
            <>
              <div className="long-text-card-paper-body"><pre className="long-text-card-body">{localLongCard.body}</pre></div>
              <footer className="long-text-card-modal-footer">
                <span className="long-text-card-count">{localLongCard.body.length.toLocaleString()} 字 · 已完整保存</span>
                {message.role === 'user' && <button className="long-text-card-button secondary" type="button" onClick={() => {setLongCardDraft(localLongCard);setEditingLongCard(true);}}>编辑这张卡片</button>}
              </footer>
            </>
          )}
        </section>
      </div>, document.body
    )}
    </>
  );
}

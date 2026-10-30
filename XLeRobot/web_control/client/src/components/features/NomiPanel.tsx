import { useEffect, useRef, useState } from 'react';
import type { Socket } from 'socket.io-client';

type SpeechRecognitionResultEvent = {
  results: ArrayLike<ArrayLike<{ transcript: string }>>;
};

type SpeechRecognitionLike = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((event: SpeechRecognitionResultEvent) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

type SpeechWindow = Window & {
  SpeechRecognition?: new () => SpeechRecognitionLike;
  webkitSpeechRecognition?: new () => SpeechRecognitionLike;
};

type TaskStatus = 'PENDING' | 'VALIDATING' | 'RUNNING' | 'NEEDS_CLARIFICATION' | 'VALIDATED' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
type TaskStep = { tool?: string; success?: boolean; message?: string; arguments?: Record<string, unknown>; data?: unknown };
type HarnessTask = {
  task_id: string;
  status?: TaskStatus;
  steps?: TaskStep[];
  error?: { code?: string; message?: string } | null;
  response_text?: string | null;
  cancelRequested?: boolean;
  submitting?: boolean;
};
type ChatMessage = { id: string; role: 'user' | 'nomi' | 'system'; text: string; taskId?: string };

const MODEL_OPTIONS = [
  { value: 'qwen35_single', label: 'Qwen3.5 9B (Single) · 기본(설치됨)' },
  { value: 'qwen_hermes', label: 'Qwen 2.5 7B + Hermes 3 8B (다운로드 필요)' },
  { value: 'doubao_pro', label: 'Doubao Pro (Ark API 키 필요)' },
  { value: 'qwen_doubao', label: 'Qwen Router + Doubao Worker' },
  { value: 'doubao_hermes', label: 'Doubao Router + Hermes Worker' },
];

const STATUS_LABEL: Record<TaskStatus, string> = {
  PENDING: 'PENDING · 대기 중',
  VALIDATING: 'VALIDATING · 검증 중',
  RUNNING: 'RUNNING · 실행 중',
  NEEDS_CLARIFICATION: 'NEEDS_CLARIFICATION · 확인 필요',
  VALIDATED: 'VALIDATED · 검증 완료 · 미실행',
  SUCCEEDED: 'SUCCEEDED · 완료',
  FAILED: 'FAILED · 실패',
  CANCELLED: 'CANCELLED · 취소됨',
};
const TERMINAL_STATUSES = new Set<TaskStatus>(['NEEDS_CLARIFICATION', 'VALIDATED', 'SUCCEEDED', 'FAILED', 'CANCELLED']);

function requestId() {
  return globalThis.crypto?.randomUUID?.() ?? `nomi-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function textFromPayload(data: unknown): string {
  if (typeof data === 'string') return data;
  if (data && typeof data === 'object' && 'message' in data) {
    const message = (data as { message?: unknown }).message;
    return typeof message === 'string' ? message : '';
  }
  return '';
}

type LogEntry = {
  id: string;
  time: string;
  level: 'info' | 'success' | 'warn' | 'error';
  message: string;
};

export function NomiPanel({ socket, theme }: { socket: Socket | null; theme: 'light' | 'dark' }) {
  const [text, setText] = useState('');
  const [validateOnly, setValidateOnly] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<Record<string, HarnessTask>>({});
  const [activeModel, setActiveModel] = useState('qwen35_single');
  const [selectedModel, setSelectedModel] = useState('qwen35_single');
  const [modelSwitchPending, setModelSwitchPending] = useState(false);
  const activeModelRef = useRef('qwen35_single');
  const modelSwitchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [bridgeConnected, setBridgeConnected] = useState(false);
  const bridgeConnectedRef = useRef<boolean | null>(null);
  const [listening, setListening] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([
    { id: 'init', time: new Date().toLocaleTimeString(), level: 'info', message: 'NOMI 제어 콘솔 초기화 완료' }
  ]);
  const [showLogs, setShowLogs] = useState(true);
  const [elapsedSec, setElapsedSec] = useState<number>(0);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const knownResponseRef = useRef(new Set<string>());
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const dark = theme === 'dark';
  const busyTask = Object.values(tasks).find(task => task.submitting || (task.status && !TERMINAL_STATUSES.has(task.status)));

  const addLog = (level: 'info' | 'success' | 'warn' | 'error', message: string) => {
    const timeStr = new Date().toLocaleTimeString();
    setLogs(previous => [...previous.slice(-40), { id: requestId(), time: timeStr, level, message }]);
  };

  useEffect(() => {
    if (busyTask) {
      const startTime = Date.now();
      timerRef.current = setInterval(() => {
        setElapsedSec(Math.round((Date.now() - startTime) / 100) / 10);
      }, 100);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      setElapsedSec(0);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [Boolean(busyTask)]);

  useEffect(() => {
    if (!socket) {
      setBridgeConnected(false);
      return;
    }
    const onTaskStatus = (data: HarnessTask) => {
      if (!data?.task_id || !data.status) return;
      setTasks(previous => ({
        ...previous,
        [data.task_id]: { ...data, submitting: false, cancelRequested: previous[data.task_id]?.cancelRequested },
      }));
      const label = STATUS_LABEL[data.status] || data.status;
      addLog(
        data.status === 'SUCCEEDED' ? 'success' : data.status === 'FAILED' ? 'error' : 'info',
        `📊 [상태 갱신] ${label} (ID: ${data.task_id.slice(0, 8)}…)`
      );
      if (data.steps && data.steps.length > 0) {
        const lastStep = data.steps[data.steps.length - 1];
        addLog('info', `🔧 [도구 실행] ${lastStep.tool || '도구'}: ${lastStep.message || (lastStep.success ? '완료' : '실행 중')}`);
      }
      if (data.response_text && !knownResponseRef.current.has(data.task_id)) {
        knownResponseRef.current.add(data.task_id);
        setMessages(previous => [...previous, {
          id: `${data.task_id}-answer`, role: 'nomi', text: data.response_text as string, taskId: data.task_id,
        }]);
      }
    };
    const onCommandReceived = (data: { status?: string; request_id?: string; text?: string }) => {
      addLog('info', `🚀 [웹 서버] ROS 2 브리지 접수 (${data.status || 'OK'}): "${data.text || ''}"`);
    };
    const onResponse = (data: unknown) => {
      const payload = data as { status?: string; message?: string; request_id?: string } | null;
      const message = textFromPayload(data);
      if (message) {
        addLog(payload?.status === 'error' ? 'error' : 'info', `💬 [응답] ${message}`);
      }
      if (message && payload?.status === 'error') {
        if (payload.request_id) {
          setTasks(previous => {
            const next = { ...previous };
            delete next[payload.request_id as string];
            return next;
          });
        }
        setMessages(previous => [...previous, { id: requestId(), role: 'system', text: message }]);
      }
    };
    const onBridgeStatus = (data: { connected?: boolean; active_model?: string }) => {
      const isConn = data?.connected === true;
      setBridgeConnected(isConn);
      if (bridgeConnectedRef.current !== isConn) {
        bridgeConnectedRef.current = isConn;
        addLog(isConn ? 'success' : 'warn', `🔗 [브리지] ${isConn ? 'NOMI Agent ROS 2 브리지 연결됨' : 'NOMI Agent 연결 끊김'}`);
      }
      if (data?.active_model) {
        setActiveModel(data.active_model);
        activeModelRef.current = data.active_model;
      }
    };
    const onModelStatus = (data: { profile?: string; status?: string; message?: string; request_id?: string; label?: string } | null) => {
      if (modelSwitchTimeoutRef.current) {
        clearTimeout(modelSwitchTimeoutRef.current);
        modelSwitchTimeoutRef.current = null;
      }
      setModelSwitchPending(false);
      if (data?.status === 'success' && data.profile) {
        const changed = data.profile !== activeModelRef.current;
        const isUserRequest = Boolean(data.request_id);
        setActiveModel(data.profile);
        activeModelRef.current = data.profile;
        setSelectedModel(data.profile);
        const label = data.label || MODEL_OPTIONS.find(option => option.value === data.profile)?.label || data.profile;
        if (changed || isUserRequest) {
          addLog('success', `🤖 [모델 변경 완료] ${label}`);
          setMessages(previous => [...previous, { id: requestId(), role: 'system', text: `모델 변경 완료: ${label}` }]);
        }
      } else {
        setSelectedModel(activeModelRef.current);
        addLog('error', `❌ [모델 변경 실패] ${data?.message || '실패'}`);
        setMessages(previous => [...previous, { id: requestId(), role: 'system', text: data?.message || '모델 변경에 실패했습니다.' }]);
      }
    };
    const onModelSwitchSent = () => {
      setModelSwitchPending(true);
      addLog('info', '⏳ [모델 변경 요청 전송 중…]');
    };
    const onCancelResult = (data: { status?: string; task_id?: string; message?: string }) => {
      if (data?.status === 'accepted' && data.task_id) {
        addLog('warn', `🛑 [작업 취소 접수] ID: ${data.task_id.slice(0, 8)}…`);
        setTasks(previous => ({ ...previous, [data.task_id as string]: { ...previous[data.task_id as string], task_id: data.task_id as string, cancelRequested: true } }));
      } else if (data?.message) {
        if (data.task_id) {
          setTasks(previous => ({ ...previous, [data.task_id as string]: { ...previous[data.task_id as string], task_id: data.task_id as string, cancelRequested: false } }));
        }
        addLog('error', `❌ [취소 오류] ${data.message}`);
        setMessages(previous => [...previous, { id: requestId(), role: 'system', text: data.message as string }]);
      }
    };
    socket.on('nomi_command_received', onCommandReceived);
    socket.on('nomi_task_status', onTaskStatus);
    socket.on('nomi_response', onResponse);
    socket.on('nomi_bridge_status', onBridgeStatus);
    socket.on('nomi_model_status', onModelStatus);
    socket.on('nomi_model_switch_sent', onModelSwitchSent);
    socket.on('nomi_cancel_result', onCancelResult);

    const checkStatus = () => {
      if (socket.connected) {
        socket.emit('nomi_get_status');
      }
    };

    socket.on('connect', checkStatus);
    checkStatus();
    const statusInterval = setInterval(checkStatus, 2500);

    return () => {
      if (modelSwitchTimeoutRef.current) {
        clearTimeout(modelSwitchTimeoutRef.current);
        modelSwitchTimeoutRef.current = null;
      }
      clearInterval(statusInterval);
      socket.off('connect', checkStatus);
      socket.off('nomi_command_received', onCommandReceived);
      socket.off('nomi_task_status', onTaskStatus);
      socket.off('nomi_response', onResponse);
      socket.off('nomi_bridge_status', onBridgeStatus);
      socket.off('nomi_model_status', onModelStatus);
      socket.off('nomi_model_switch_sent', onModelSwitchSent);
      socket.off('nomi_cancel_result', onCancelResult);
    };
  }, [socket]);

  useEffect(() => {
    const speech = recognitionRef.current;
    return () => speech?.stop();
  }, []);

  const sendCommand = () => {
    const command = text.trim();
    if (!command || !socket?.connected || busyTask) return;
    const id = requestId();
    addLog('info', `📤 [요청 전송] "${command}" (ID: ${id.slice(0, 8)}…)`);
    setMessages(previous => [...previous, { id: `${id}-user`, role: 'user', text: command, taskId: id }]);
    setTasks(previous => ({ ...previous, [id]: { task_id: id, submitting: true } }));
    socket.emit('nomi_command', { text: command, request_id: id, validate_only: validateOnly });
    setText('');
  };

  const switchModel = () => {
    if (!socket?.connected || !bridgeConnected || !selectedModel || (selectedModel === activeModel) || modelSwitchPending) return;
    setModelSwitchPending(true);
    if (modelSwitchTimeoutRef.current) clearTimeout(modelSwitchTimeoutRef.current);
    modelSwitchTimeoutRef.current = setTimeout(() => {
      setModelSwitchPending(false);
      addLog('warn', '⚠️ 모델 변경 응답 시간 초과 (다시 시도해주세요)');
    }, 6000);
    socket.emit('nomi_model_switch', { profile: selectedModel, request_id: requestId() });
  };

  const cancelTask = (taskId: string) => {
    if (!socket?.connected) return;
    socket.emit('nomi_task_cancel', { task_id: taskId });
  };

  const toggleSpeech = () => {
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const speech = window as SpeechWindow;
    const Recognition = speech.SpeechRecognition || speech.webkitSpeechRecognition;
    if (!Recognition) {
      setMessages(previous => [...previous, { id: requestId(), role: 'system', text: '이 브라우저는 음성 인식을 지원하지 않습니다.' }]);
      return;
    }
    const recognition = new Recognition();
    recognition.lang = 'ko-KR';
    recognition.interimResults = false;
    recognition.continuous = false;
    recognition.onresult = event => {
      const transcript = event.results[0]?.[0]?.transcript?.trim();
      if (transcript) setText(transcript);
    };
    recognition.onerror = () => setMessages(previous => [...previous, { id: requestId(), role: 'system', text: '음성 인식을 완료하지 못했습니다.' }]);
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;
    setListening(true);
    recognition.start();
  };

  return (
    <section className={`surface-card rounded-2xl border p-4 shadow-sm ${dark ? 'dark-panel text-gray-100' : 'bg-white text-gray-900'}`}>
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold">NOMI AI</h2>
          <p className="text-xs opacity-70">명령은 NOMI Execution Harness에서 검증·실행됩니다.</p>
        </div>
        <div className="flex flex-col items-end gap-1 text-right">
          <span className={`rounded-full px-2 py-1 text-xs ${socket?.connected ? 'bg-green-100 text-green-800' : 'bg-gray-200 text-gray-700'}`}>
            {socket?.connected ? '웹 서버 연결됨' : '웹 서버 연결 안 됨'}
          </span>
          <div className="flex items-center gap-1">
            <span className={`rounded-full px-2 py-1 text-xs ${bridgeConnected ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'}`}>
              {bridgeConnected ? '🟢 NOMI Agent 연결됨' : '🟡 NOMI Agent 연결 확인 중'}
            </span>
            <button
              onClick={() => { if (socket?.connected) socket.emit('nomi_get_status'); }}
              title="Agent 브리지 연결 상태 즉시 새로고침"
              className="rounded p-1 text-xs hover:bg-slate-200 dark:hover:bg-gray-700"
            >
              🔄
            </button>
          </div>
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select value={selectedModel || activeModel} onChange={event => setSelectedModel(event.target.value)} className="model-select min-w-0 flex-1 rounded-xl border px-3 py-2 text-sm text-gray-900" aria-label="NOMI 모델 선택">
          {MODEL_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <button onClick={switchModel} disabled={!socket?.connected || !bridgeConnected || !selectedModel || (selectedModel === activeModel) || modelSwitchPending} className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 disabled:opacity-50">
          {modelSwitchPending ? '확인 중…' : '변경'}
        </button>
        <span className="w-full text-xs opacity-70 sm:w-auto sm:flex-1 sm:text-right">
          활성 모델: {MODEL_OPTIONS.find(option => option.value === activeModel)?.label || activeModel || 'qwen3.5:9b'}
        </span>
      </div>

      <div aria-live="polite" className={`mb-3 h-56 space-y-2 overflow-y-auto rounded-xl border p-3 text-sm ${dark ? 'border-gray-700' : 'border-gray-200 bg-slate-50'}`}>
        {messages.length === 0 ? <p className="opacity-50">아직 대화가 없습니다.</p> : messages.map(message => (
          <div key={message.id} className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[92%] rounded-2xl px-3 py-2 ${message.role === 'user' ? 'bg-blue-600 text-white' : message.role === 'system' ? 'border border-amber-200 bg-amber-50 text-amber-900' : dark ? 'bg-gray-800' : 'bg-white shadow-sm'}`}>
              <strong className="mr-1 text-xs opacity-70">{message.role === 'user' ? '사용자' : message.role === 'nomi' ? 'NOMI' : '알림'}</strong>
              <span className="whitespace-pre-wrap break-words">{message.text}</span>
              {message.taskId && message.role === 'user' && <span className="mt-1 block break-all text-[10px] opacity-65">작업 ID: {message.taskId}</span>}
            </div>
          </div>
        ))}
      </div>

      {busyTask && (
        <div className="mb-3 flex items-center justify-between rounded-xl border border-blue-200 bg-blue-50/80 px-3 py-2 text-xs font-medium text-blue-800 backdrop-blur-sm dark:border-blue-900/50 dark:bg-blue-950/40 dark:text-blue-300">
          <span className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-500" />
            </span>
            <span>{busyTask.status ? STATUS_LABEL[busyTask.status] : '요청 전송 및 AI 처리 중…'}</span>
          </span>
          <span className="font-mono text-xs font-semibold text-blue-900 dark:text-blue-200">
            ⏱️ 경과: {elapsedSec.toFixed(1)}s
          </span>
        </div>
      )}

      {Object.values(tasks).slice(-4).map(task => {
        const status = task.status;
        const terminal = status ? TERMINAL_STATUSES.has(status) : false;
        const steps = 'steps' in task ? task.steps ?? [] : [];
        const error = 'error' in task ? task.error : undefined;
        return (
          <div key={task.task_id} className="mb-2 rounded-xl border border-slate-200 p-3 text-xs dark:border-gray-700">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <strong>{status ? STATUS_LABEL[status] : '요청 전송 중'}</strong>
                <span className="ml-2 break-all opacity-60">ID: {task.task_id}</span>
              </div>
              {status && !terminal && <button onClick={() => cancelTask(task.task_id)} disabled={!socket?.connected || task.cancelRequested} className="rounded-lg border border-rose-300 px-2 py-1 font-semibold text-rose-700 disabled:opacity-50">
                {task.cancelRequested ? '취소 확인 중…' : '작업 취소'}
              </button>}
            </div>
            {steps.length > 0 && <ul className="mt-2 space-y-1">
              {steps.map((step, index) => <li key={`${task.task_id}-step-${index}`} className={step.success ? 'text-emerald-700 dark:text-emerald-300' : 'text-rose-700 dark:text-rose-300'}>
                {step.success ? '✓' : '•'} {step.tool || '도구'}{step.message ? ` — ${step.message}` : ''}
              </li>)}
            </ul>}
            {error?.message && <p className="mt-2 text-rose-700 dark:text-rose-300">오류{error.code ? ` (${error.code})` : ''}: {error.message}</p>}
          </div>
        );
      })}

      <div className="flex items-end gap-2">
        <button aria-label={listening ? '음성 인식 중지' : '음성 입력'} onClick={toggleSpeech} className={`rounded-xl px-3 py-2 transition ${listening ? 'bg-rose-600 text-white' : 'border border-blue-100 bg-blue-50 text-blue-700 hover:bg-blue-100'}`}>
          {listening ? '■' : '🎤'}
        </button>
        <textarea
          value={text}
          onChange={event => setText(event.target.value)}
          onKeyDown={event => {
            event.stopPropagation();
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              sendCommand();
            }
          }}
          placeholder="예: 분홍색 수건을 집어서 테이블 위에 놓아 줘"
          aria-label="NOMI 채팅 메시지"
          rows={1}
          className="chat-input min-w-0 flex-1 resize-y rounded-xl border px-3 py-2 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
        />
        <button onClick={sendCommand} disabled={!socket?.connected || !bridgeConnected || !text.trim() || Boolean(busyTask)} className="rounded-xl bg-blue-600 px-4 py-2 font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50">전송</button>
      </div>
      <label className="mt-2 flex items-center gap-2 text-xs text-amber-800 dark:text-amber-300">
        <input type="checkbox" checked={validateOnly} onChange={event => setValidateOnly(event.target.checked)} />
        검증 전용 · 도구 호출을 검증하고 실행하지 않음
      </label>
      {!bridgeConnected && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">NOMI Agent 브리지 연결이 확인되지 않아 AI 명령과 모델 변경을 사용할 수 없습니다.</p>}

      <div className="mt-3 overflow-hidden rounded-xl border border-slate-200 dark:border-gray-700">
        <div
          onClick={() => setShowLogs(!showLogs)}
          className="flex cursor-pointer select-none items-center justify-between bg-slate-100 px-3 py-2 text-xs font-semibold text-slate-700 transition hover:bg-slate-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-750"
        >
          <span className="flex items-center gap-1.5">
            <span>📡 실시간 요청 전송 로그</span>
            <span className="rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] font-bold text-blue-700 dark:bg-blue-900/50 dark:text-blue-300">
              {logs.length}
            </span>
          </span>
          <span className="text-[11px] font-normal opacity-70">
            {showLogs ? '접기 ▲' : '펼치기 ▼'}
          </span>
        </div>
        {showLogs && (
          <div className="max-h-44 space-y-1 overflow-y-auto bg-slate-900 p-2.5 font-mono text-[11px] text-slate-200 shadow-inner">
            {logs.map(log => (
              <div key={log.id} className="flex items-start gap-1.5 leading-relaxed">
                <span className="shrink-0 text-slate-500">[{log.time}]</span>
                <span className={
                  log.level === 'success' ? 'text-emerald-400' :
                  log.level === 'error' ? 'text-rose-400' :
                  log.level === 'warn' ? 'text-amber-400' :
                  'text-blue-300'
                }>{log.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <p className="mt-2 text-[10px] opacity-60">작업 취소는 Harness 작업 취소 요청입니다. 긴급 정지는 조종 패널의 별도 정지 기능을 사용하세요.</p>
    </section>
  );
}

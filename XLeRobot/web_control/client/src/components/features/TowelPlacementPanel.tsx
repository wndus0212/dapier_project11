import { useCallback, useEffect, useState } from 'react';
import { ENV } from '../../config/environment';

type Features = { cx: number; cy: number; area: number; angle: number };
type State = { ready: boolean; reason: string; image: string | null; reference_image: string | null;
  actual: Features | null; reference: Features | null; distance_mm: number | null;
  reference_distance_mm: number | null; tolerances: { center_px: number | null; area_ratio: number | null; angle_deg: number | null } };

const reasons: Record<string, string> = {
  ready: '실행 준비 완료', placement_not_calibrated_or_missing: '기준 배치를 등록해 주세요',
  placement_invalid_config: '배치 허용 오차 설정이 필요합니다', placement_out_of_range: '수건 배치를 기준에 맞춰 주세요',
  placement_detection_failed: '수건을 인식하지 못했습니다', towel_not_spread: '수건을 펼쳐 주세요',
  camera_stale: '카메라 영상이 없거나 오래되었습니다', not_calibrated: '작업대 기준 거리 보정이 필요합니다',
};
function explain(reason: string) { return reasons[reason.split(':')[0]] ?? reason; }
function number(value: number | null | undefined, suffix = '') { return value == null || !Number.isFinite(value) ? '미설정' : `${value.toFixed(1)}${suffix}`; }

export function TowelPlacementPanel() {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [limits, setLimits] = useState({ center_px: '', area_percent: '', angle_deg: '' });
  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${ENV.SERVER_URL}/api/towel/viewer`, { cache: 'no-store' });
      if (!response.ok) throw new Error('카메라 또는 수건 API 연결을 확인해 주세요');
      setState(await response.json() as State); setError('');
    } catch { setState(null); setError('카메라 또는 수건 API 연결을 확인해 주세요'); }
  }, []);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 1500); return () => window.clearInterval(timer); }, [refresh]);
  const save = async () => {
    if (!window.confirm('학습 당시와 같은 수건·로봇·카메라 시작 배치인가요? 현재 화면을 기준으로 저장합니다.')) return;
    setSaving(true); setMessage('');
    try {
      const response = await fetch(`${ENV.SERVER_URL}/api/towel/reference`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true }) });
      const result = await response.json() as { saved?: boolean; reason?: string; detail?: string; tolerances_configured?: boolean };
      setMessage(result.saved ? (result.tolerances_configured ? '기준 배치를 저장했습니다.' : '기준을 저장했습니다. 측정된 배치 허용 오차 설정이 필요합니다.') : explain(result.reason ?? result.detail ?? '기준을 저장하지 못했습니다'));
      await refresh();
    } catch { setMessage('기준 저장 요청에 실패했습니다.'); } finally { setSaving(false); }
  };
  const a = state?.actual; const r = state?.reference; const t = state?.tolerances;
  const saveLimits = async () => {
    if (!window.confirm('실제로 측정하고 검증한 허용 오차를 저장하시겠습니까?')) return;
    const values = { center_px: Number(limits.center_px), area_ratio: Number(limits.area_percent) / 100, angle_deg: Number(limits.angle_deg), confirm: true };
    if (Object.values(limits).some(value => !value.trim()) || Object.values(values).some(value => typeof value === 'number' && (!Number.isFinite(value) || value <= 0))) { setMessage('측정된 양수 값을 모두 입력해 주세요.'); return; }
    try {
      const response = await fetch(`${ENV.SERVER_URL}/api/towel/tolerances`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values) });
      const result = await response.json() as { saved?: boolean; reason?: string; detail?: string };
      setMessage(result.saved ? '허용 오차를 저장했습니다.' : explain(result.reason ?? result.detail ?? '허용 오차 저장 실패'));
      await refresh();
    } catch { setMessage('허용 오차 저장 요청에 실패했습니다.'); }
  };
  const center = a && r ? Math.hypot(a.cx-r.cx, a.cy-r.cy) : null;
  const angle = a && r ? Math.abs(((a.angle-r.angle+270)%180)-90) : null;
  const area = a && r && r.area ? Math.abs(a.area/r.area-1)*100 : null;
  return <section className="surface-card rounded-2xl border p-4 space-y-3" aria-label="수건 배치 확인">
    <div className="flex justify-between gap-2"><h2 className="font-semibold">수건 시작 배치 확인</h2><span className={state?.ready ? 'text-green-600' : 'text-amber-600'}>{state?.ready ? '준비 완료' : '확인 필요'}</span></div>
    <p className="text-sm">{error || (state ? explain(state.reason) : '카메라 연결 중')}</p>
    <div className="relative aspect-[4/3] overflow-hidden rounded-xl bg-slate-900">
      {state?.image && <img className="absolute inset-0 h-full w-full object-contain" src={`data:image/png;base64,${state.image}`} alt="Astra 현재 영상" />}
      {state?.image && state.reference_image && <img className="absolute inset-0 h-full w-full object-contain opacity-40" src={`data:image/png;base64,${state.reference_image}`} alt="기준 배치 겹쳐 보기" />}
      {!state?.image && <span className="absolute inset-0 grid place-items-center text-sm text-white">영상 대기 중</span>}
    </div>
    <p className="text-xs text-slate-500">기준 영상은 반투명으로 표시됩니다. 영상 기준 위치는 320×240 픽셀입니다.</p>
    <dl className="grid grid-cols-2 gap-2 text-xs">
      <div>위치 차이: {number(center, ' px')} / 허용 {number(t?.center_px, ' px')}</div>
      <div>방향 차이: {number(angle, '°')} / 허용 {number(t?.angle_deg, '°')}</div>
      <div>면적 차이: {number(area, '%')} / 허용 {number(t?.area_ratio == null ? null : t.area_ratio*100, '%')}</div>
      <div>작업대 거리: {number(state?.distance_mm, ' mm')} / 기준 {number(state?.reference_distance_mm, ' mm')}</div>
    </dl>
    <button type="button" disabled={saving || !state?.image} onClick={() => void save()} className="rounded-lg bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50">{saving ? '저장 중' : '현재 배치를 기준으로 등록'}</button>
    <details className="text-sm"><summary>측정된 배치 허용 오차 설정</summary><p className="my-2 text-xs">반드시 실물 시험으로 확인한 값을 입력하세요. 미설정 시 실행이 거부됩니다.</p>
      <div className="grid grid-cols-3 gap-2">{([['center_px', '위치 px'], ['area_percent', '면적 %'], ['angle_deg', '방향 °']] as const).map(([key, label]) => <label key={key} className="text-xs">{label}<input type="number" min="0.01" step="any" value={limits[key]} onChange={event => setLimits(previous => ({...previous, [key]: event.target.value}))} className="w-full rounded border p-1 text-slate-900" /></label>)}</div>
      <button type="button" onClick={() => void saveLimits()} className="mt-2 rounded border px-3 py-1">측정값 저장</button>
    </details>
    {message && <p role="status" className="text-sm">{message}</p>}
  </section>;
}

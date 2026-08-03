import React, { useState, useEffect } from 'react';
import { Save, AlertTriangle, Sparkles, Check, FileText } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from '../../components/Toast';

export default function MaintenanceSection({ settings, onSave }) {
  const { t } = useTranslation();
  const [bloatMinBytes, setBloatMinBytes] = useState(
    settings?.bloat_min_bytes != null ? String(settings.bloat_min_bytes) : ''
  );
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings?.bloat_min_bytes != null) {
      setBloatMinBytes(String(settings.bloat_min_bytes));
      setDirty(false);
    }
  }, [settings?.bloat_min_bytes]);

  const parsedVal = parseInt(bloatMinBytes, 10);
  const isValid = !isNaN(parsedVal) && parsedVal >= 1 && String(parsedVal) === bloatMinBytes.trim();

  const formatByteHint = (bytes) => {
    if (isNaN(bytes) || bytes < 1) return null;
    if (bytes >= 1048576) {
      return t('settings.maintenance.equiv_mb', { mb: (bytes / 1048576).toFixed(2) });
    }
    if (bytes >= 1024) {
      return t('settings.maintenance.equiv_kb', { kb: (bytes / 1024).toFixed(1) });
    }
    return `${bytes} ${t('settings.maintenance.bytes_unit')}`;
  };

  const getCharEstimate = (bytes) => {
    if (isNaN(bytes) || bytes < 1) return null;
    const zhCount = Math.round(bytes / 3);
    const enCount = bytes;
    return t('settings.maintenance.char_estimate', { zhCount, enCount });
  };

  const handleSave = async () => {
    if (!isValid) {
      toast(t('settings.maintenance.invalid_bloat_min_bytes'), 'error');
      return;
    }
    setSaving(true);
    try {
      await onSave({ bloat_min_bytes: parsedVal });
      setDirty(false);
    } catch (e) {
      toast(
        t('settings.maintenance.save_failed') +
          ': ' +
          (e.response?.data?.detail || e.message),
        'error'
      );
    } finally {
      setSaving(false);
    }
  };

  const PRESETS = [
    { bytes: 1200, labelKey: 'preset_strict', descKey: 'preset_strict_desc' },
    { bytes: 2400, labelKey: 'preset_standard', descKey: 'preset_standard_desc' },
    { bytes: 4800, labelKey: 'preset_relaxed', descKey: 'preset_relaxed_desc' },
  ];

  const handleApplyPreset = (bytes) => {
    setBloatMinBytes(String(bytes));
    const savedVal = settings?.bloat_min_bytes != null ? String(settings.bloat_min_bytes) : '';
    setDirty(String(bytes) !== savedVal);
  };

  return (
    <div className="space-y-5 pt-2">
      {/* Explanation Banner */}
      <div className="p-4 rounded-xl bg-indigo-950/40 border border-indigo-800/40 text-slate-300 text-xs leading-relaxed space-y-2 shadow-inner">
        <div className="flex items-center gap-2 text-indigo-300 font-semibold text-sm">
          <Sparkles size={16} className="text-indigo-400 flex-shrink-0" />
          <span>{t('settings.maintenance.explanation_title')}</span>
        </div>
        <p className="text-slate-300/90 leading-normal">
          {t('settings.maintenance.explanation_text')}
        </p>
        <div className="pt-1 flex items-center gap-1.5 text-[11px] text-indigo-300/80 font-mono">
          <FileText size={13} className="text-indigo-400 flex-shrink-0" />
          <span>docs/skills/memory-audit/SKILL.md &rarr; system://diagnostic</span>
        </div>
      </div>

      {/* Main Setting Input & Presets */}
      <div className="space-y-3 pt-1">
        <div className="space-y-1">
          <label className="block text-xs font-medium text-slate-300 flex items-center justify-between">
            <span className="font-semibold text-slate-200">{t('settings.maintenance.bloat_min_bytes_label')}</span>
            {isValid && (
              <span className="text-[11px] font-mono text-indigo-400 font-normal">
                {formatByteHint(parsedVal)} {getCharEstimate(parsedVal)}
              </span>
            )}
          </label>
          <p className="text-xs text-slate-400 leading-relaxed">
            {t('settings.maintenance.bloat_min_bytes_desc')}
          </p>
        </div>

        {/* Preset Chips */}
        <div className="space-y-1.5 pt-1">
          <div className="text-[11px] font-medium text-slate-400">
            {t('settings.maintenance.presets_label')}
          </div>
          <div className="grid grid-cols-3 gap-2">
            {PRESETS.map((p) => {
              const isSelected = isValid && parsedVal === p.bytes;
              return (
                <button
                  key={p.bytes}
                  type="button"
                  onClick={() => handleApplyPreset(p.bytes)}
                  className={`p-2 rounded-lg border text-left transition-all flex flex-col justify-between ${
                    isSelected
                      ? 'bg-indigo-600/20 border-indigo-500/80 text-indigo-200 ring-1 ring-indigo-500/30'
                      : 'bg-slate-900/60 border-slate-800 text-slate-300 hover:bg-slate-800/60 hover:border-slate-700'
                  }`}
                >
                  <div className="flex items-center justify-between w-full font-medium text-xs">
                    <span>{t(`settings.maintenance.${p.labelKey}`)}</span>
                    {isSelected && <Check size={12} className="text-indigo-400 flex-shrink-0" />}
                  </div>
                  <div className="text-[10px] text-slate-400 mt-1">
                    {t(`settings.maintenance.${p.descKey}`)}
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Manual Input */}
        <div className="pt-2">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min="1"
              value={bloatMinBytes}
              onChange={(e) => {
                const val = e.target.value;
                setBloatMinBytes(val);
                const savedVal = settings?.bloat_min_bytes != null ? String(settings.bloat_min_bytes) : '';
                setDirty(val !== savedVal);
              }}
              placeholder={settings?.bloat_min_bytes != null ? String(settings.bloat_min_bytes) : ''}
              className="bg-slate-950 border border-slate-700 text-slate-200 rounded-lg px-3 py-2 text-sm w-44 font-mono focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 shadow-inner"
            />
            <span className="text-xs text-slate-500 font-mono">
              {t('settings.maintenance.bytes_unit')}
            </span>
          </div>
        </div>

        {!isValid && dirty && (
          <p className="text-xs text-rose-400 flex items-center gap-1 pt-1">
            <AlertTriangle size={12} />
            {t('settings.maintenance.invalid_bloat_min_bytes')}
          </p>
        )}

        {/* Tip */}
        <p className="text-[11px] text-slate-400/80 bg-slate-900/40 p-2.5 rounded-lg border border-slate-800/50 leading-relaxed">
          {t('settings.maintenance.impact_hint')}
        </p>
      </div>

      {dirty && (
        <div className="flex items-center justify-end pt-2">
          <button
            onClick={handleSave}
            disabled={saving || !isValid}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium flex items-center gap-2 transition-colors shadow-lg shadow-indigo-900/20"
          >
            <Save size={14} />
            {saving ? t('settings.maintenance.saving') : t('settings.maintenance.save')}
          </button>
        </div>
      )}
    </div>
  );
}

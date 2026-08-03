import React, { useState, useEffect } from 'react';
import { Save, AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from '../../components/Toast';

export default function MaintenanceSection({ settings, onSave }) {
  const { t } = useTranslation();
  const [bloatMinBytes, setBloatMinBytes] = useState('2048');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings?.bloat_min_bytes != null) {
      setBloatMinBytes(String(settings.bloat_min_bytes));
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

  return (
    <div className="space-y-4 pt-4">
      <div className="space-y-2">
        <label className="block text-xs font-medium text-slate-400 uppercase tracking-wider flex items-center justify-between">
          <span>{t('settings.maintenance.bloat_min_bytes_label')}</span>
          {isValid && (
            <span className="text-[11px] font-mono text-indigo-400 font-normal">
              {formatByteHint(parsedVal)}
            </span>
          )}
        </label>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min="1"
            value={bloatMinBytes}
            onChange={(e) => {
              setBloatMinBytes(e.target.value);
              setDirty(true);
            }}
            placeholder="2048"
            className="bg-slate-950 border border-slate-700 text-slate-200 rounded-lg px-3 py-2 text-sm w-44 font-mono focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 shadow-inner"
          />
          <span className="text-xs text-slate-500 font-mono">
            {t('settings.maintenance.bytes_unit')}
          </span>
        </div>
        <p className="text-xs text-slate-500 leading-relaxed pt-1">
          {t('settings.maintenance.bloat_min_bytes_desc')}
        </p>
        {!isValid && dirty && (
          <p className="text-xs text-rose-400 flex items-center gap-1 pt-1">
            <AlertTriangle size={12} />
            {t('settings.maintenance.invalid_bloat_min_bytes')}
          </p>
        )}
      </div>

      {dirty && (
        <div className="flex items-center justify-end pt-1">
          <button
            onClick={handleSave}
            disabled={saving || !isValid}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium flex items-center gap-2 transition-colors"
          >
            <Save size={14} />
            {saving ? t('settings.maintenance.saving') : t('settings.maintenance.save')}
          </button>
        </div>
      )}
    </div>
  );
}

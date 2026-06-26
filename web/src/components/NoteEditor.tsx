import { useEffect, useRef, useState } from 'react';
import './NoteEditor.css';

interface Props {
  /** Current saved note (null/'' = none). */
  value: string | null | undefined;
  /** Persist a new note body (empty string deletes). */
  onSave: (body: string) => void | Promise<void>;
  /** Compact variant for cards. */
  compact?: boolean;
}

/** One editable note per post. Shows the note, click to edit; saves on blur / Cmd+Enter. */
export function NoteEditor({ value, onSave, compact }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? '');
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { setDraft(value ?? ''); }, [value]);

  useEffect(() => {
    if (editing && taRef.current) {
      taRef.current.focus();
      taRef.current.setSelectionRange(taRef.current.value.length, taRef.current.value.length);
    }
  }, [editing]);

  const commit = () => {
    setEditing(false);
    if ((draft ?? '') !== (value ?? '')) onSave(draft.trim());
  };

  if (editing) {
    return (
      <div className={`note-editor${compact ? ' note-editor--compact' : ''}`} onClick={(e) => e.stopPropagation()}>
        <textarea
          ref={taRef}
          className="note-textarea"
          value={draft}
          placeholder="Add a note…"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) commit();
            if (e.key === 'Escape') { setDraft(value ?? ''); setEditing(false); }
          }}
        />
      </div>
    );
  }

  const has = !!(value && value.trim());
  return (
    <div
      className={`note-display${has ? ' note-display--has' : ''}${compact ? ' note-display--compact' : ''}`}
      onClick={(e) => { e.stopPropagation(); setEditing(true); }}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); setEditing(true); } }}
      title={has ? 'Click to edit note' : 'Add a note'}
    >
      <span className="note-icon">✎</span>
      {has ? <span className="note-text">{value}</span> : <span className="note-placeholder">Add a note…</span>}
    </div>
  );
}

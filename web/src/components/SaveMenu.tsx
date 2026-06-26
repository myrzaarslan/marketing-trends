import { useEffect, useRef, useState } from 'react';
import type { Collection } from '../types';
import './SaveMenu.css';

interface Props {
  collections: Collection[];
  memberIds: number[];
  onToggle: (collectionId: number, makeMember: boolean) => void;
  onCreate: (title: string) => Promise<Collection | null>;
  onClose: () => void;
}

/** Popover for adding/removing a post to/from collections, with inline create. */
export function SaveMenu({ collections, memberIds, onToggle, onCreate, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const members = new Set(memberIds);

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onEsc = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onEsc);
    };
  }, [onClose]);

  const submitCreate = async () => {
    const t = title.trim();
    if (!t || busy) return;
    setBusy(true);
    const created = await onCreate(t);
    setBusy(false);
    if (created) {
      onToggle(created.id, true);
      setTitle('');
      setCreating(false);
    }
  };

  return (
    <div className="save-menu" ref={ref} onClick={(e) => e.stopPropagation()}>
      <div className="save-menu-head">Save to collection</div>
      <div className="save-menu-list">
        {collections.length === 0 && !creating && (
          <div className="save-menu-empty">No collections yet.</div>
        )}
        {collections.map((c) => {
          const isMember = members.has(c.id);
          return (
            <button
              key={c.id}
              type="button"
              className={`save-menu-item${isMember ? ' save-menu-item--on' : ''}`}
              onClick={() => onToggle(c.id, !isMember)}
            >
              <span className="save-menu-check">{isMember ? '✓' : ''}</span>
              <span className="save-menu-title">{c.title}</span>
              <span className="save-menu-count">{c.item_count}</span>
            </button>
          );
        })}
      </div>

      {creating ? (
        <div className="save-menu-create">
          <input
            autoFocus
            className="save-menu-input"
            placeholder="New collection name…"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitCreate();
              if (e.key === 'Escape') { setCreating(false); setTitle(''); }
            }}
          />
          <button type="button" className="save-menu-add" disabled={busy} onClick={submitCreate}>
            Add
          </button>
        </div>
      ) : (
        <button type="button" className="save-menu-newbtn" onClick={() => setCreating(true)}>
          + New collection
        </button>
      )}
    </div>
  );
}

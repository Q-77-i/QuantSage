"use client";

import { groupNotes, noteParts } from "@/lib/factor-report";
import type { FactorReport } from "@/lib/types";

/**
 * 如实标注清单：**服务端 notes 原样渲染**，前端只分组、不增删一个字。
 *
 * 免责与口径文案若在前后端各写一份，两处必然漂移——服务端那份是「进决策的数据必须带来源
 * 与口径」的一部分，前端只负责把它排开、让人读得下去（十来条平铺是一堵墙）。
 */
export function NotesList({ notes }: { notes: FactorReport["notes"] }) {
  const groups = groupNotes(notes);
  if (groups.length === 0) return null;

  return (
    <div className="mt-3 flex flex-col gap-3">
      {groups.map((group) => (
        <div key={group.title}>
          <p className="text-xs font-medium text-ink-2">{group.title}</p>
          <ul className="mt-1 flex list-disc flex-col gap-1 pl-5 text-xs text-ink-3">
            {group.items.map((note) => (
              <li key={note}>
                {noteParts(note).map((part, index) =>
                  part.strong ? (
                    <strong key={index} className="font-medium text-ink-2">
                      {part.text}
                    </strong>
                  ) : (
                    <span key={index}>{part.text}</span>
                  ),
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

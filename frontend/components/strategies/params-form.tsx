"use client";

import { paramFields, type ParamField, type ParamValue } from "@/lib/strategy-form";
import type { StrategyMeta } from "@/lib/types";

/**
 * 参数表单：由 `PARAMS` schema 生成（M4c）。
 *
 * **值域规则不重复造**（SPEC §5 M4c）：`min` / `max` 只作**提示**印在输入框旁，不在前端拦；
 * 提交后由后端按同一份 schema 校验并给中文话术。前端拦一遍的代价是两处规则必然漂移——
 * 而且拦错了用户还没法绕过。
 *
 * 表单值一律以**字符串**保存（忠实反映用户敲的内容，空串也照传，由后端报「需要数字」）；
 * 开关是布尔。
 */
export function ParamsForm({
  meta,
  values,
  onChange,
}: {
  meta: StrategyMeta | null;
  values: Record<string, ParamValue>;
  onChange: (values: Record<string, ParamValue>) => void;
}) {
  const fields = paramFields(meta);

  if (fields.length === 0) {
    return (
      <p className="text-xs text-ink-3">
        {meta
          ? "这份策略没有声明参数（PARAMS 为空）。"
          : "读不到 PARAMS：检查结果里有一条 R4，修好它就会出现参数表单。"}
      </p>
    );
  }

  return (
    <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
      {fields.map((field) => (
        <Field
          key={field.key}
          field={field}
          value={values[field.key]}
          onChange={(next) => onChange({ ...values, [field.key]: next })}
        />
      ))}
    </div>
  );
}

function Field({
  field,
  value,
  onChange,
}: {
  field: ParamField;
  value: ParamValue | undefined;
  onChange: (value: ParamValue) => void;
}) {
  if (field.type === "bool") {
    return (
      <label className="flex cursor-pointer items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(event.target.checked)}
          className="size-4 accent-[var(--brand)]"
        />
        {field.label}
      </label>
    );
  }

  const hint = rangeHint(field);
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-ink-2">{field.label}</span>
      <span className="flex items-center gap-2">
        <input
          type="number"
          inputMode="decimal"
          step={field.type === "int" ? 1 : "any"}
          value={String(value ?? "")}
          onChange={(event) => onChange(event.target.value)}
          className="num w-28 rounded-[var(--radius)] border border-border bg-surface px-2 py-1 text-sm outline-none focus:border-brand"
        />
        {hint ? <span className="num text-xs text-ink-3">{hint}</span> : null}
      </span>
    </label>
  );
}

/** `min ~ max` 提示（只显示，不拦截） */
function rangeHint(field: ParamField): string {
  if (field.min === null && field.max === null) return "";
  const low = field.min ?? "不限";
  const high = field.max ?? "不限";
  return `${low} ~ ${high}`;
}

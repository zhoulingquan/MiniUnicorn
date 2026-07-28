import { useEffect, useMemo, useState } from "react";
import { Loader2, Save, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ToggleSwitch } from "@/components/ui/toggle-switch";
import { QrcodeAuthBlock } from "@/components/channels/QrcodeAuthBlock";
import type { ChannelFieldSchema, ChannelPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 字段标签/描述的 i18n helper（与 ChannelsView 共用逻辑）。 */
function useFieldLabels() {
  const { t } = useTranslation();
  return (channelName: string, field: ChannelFieldSchema): {
    label: string;
    description: string;
  } => {
    const labelKey = `channels.channelFields.${channelName}.${field.name}`;
    const labelFallbackKey = `channels.channelFields._common.${field.name}`;
    const translated = t(labelKey, { defaultValue: "" });
    const fallbackTranslated = t(labelFallbackKey, { defaultValue: "" });
    const label = translated || fallbackTranslated || field.label;

    const descKey = `channels.channelFields.${channelName}.${field.name}_desc`;
    const descFallbackKey = `channels.channelFields._common.${field.name}_desc`;
    const descTranslated = t(descKey, { defaultValue: "" });
    const descFallbackTranslated = t(descFallbackKey, { defaultValue: "" });
    const description = descTranslated || descFallbackTranslated || field.description;

    return { label, description };
  };
}

type FormValues = Record<string, unknown>;

function initFormValues(ch: ChannelPayload): FormValues {
  const base = (ch.config ?? ch.default_config ?? {}) as Record<string, unknown>;
  const values: FormValues = {};
  for (const f of ch.config_schema ?? []) {
    values[f.name] = f.name in base ? base[f.name] : f.default;
  }
  return values;
}

function listToDraft(value: unknown): string {
  return Array.isArray(value) ? value.join("\n") : "";
}

function draftToList(draft: string): string[] {
  return draft
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function valuesEqual(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
  }
  return a === b;
}

interface ChannelDrawerProps {
  channel: ChannelPayload | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  displayName: string;
  description: string;
  acting: boolean;
  /** WebUI API token，传给 QrcodeAuthBlock 用于扫码登录 API 调用。 */
  token: string;
  onSave: (payload: Record<string, unknown>) => void;
  onDelete: () => void;
  /** 扫码登录成功后的回调（参数为后端写入的最新 channel config dict）。 */
  onQrSuccess: (config: Record<string, unknown>) => void;
}

/**
 * 频道配置弹窗：居中 Dialog，按 channel.config_schema 动态渲染表单。
 * 样式与 SkillsView 的"添加技能"编辑弹窗保持一致（Dialog + max-w-3xl +
 * DialogHeader + 主体 + DialogFooter）。
 *
 * 当 ``channel.qr_login_supported`` 为 true 时，在表单上方显示
 * QrcodeAuthBlock 扫码登录块（参考 QwenPaw ChannelDrawer 的扫码区）。
 */
export function ChannelDrawer({
  channel,
  open,
  onOpenChange,
  displayName,
  description,
  acting,
  token,
  onSave,
  onDelete,
  onQrSuccess,
}: ChannelDrawerProps) {
  const { t } = useTranslation();
  const getFieldLabels = useFieldLabels();
  const [formValues, setFormValues] = useState<FormValues>({});
  const [listDrafts, setListDrafts] = useState<Record<string, string>>({});

  // channel 切换时重置表单
  useEffect(() => {
    if (channel) {
      setFormValues(initFormValues(channel));
      setListDrafts({});
    }
  }, [channel]);

  const schema = useMemo(() => channel?.config_schema ?? [], [channel]);
  const isDirty = useMemo(() => {
    if (!channel || schema.length === 0) return false;
    const original = (channel.config ?? channel.default_config ?? {}) as Record<string, unknown>;
    for (const f of schema) {
      const cur = formValues[f.name];
      const orig = f.name in original ? original[f.name] : f.default;
      if (!valuesEqual(cur, orig)) return true;
    }
    return false;
  }, [channel, schema, formValues]);

  if (!channel) return null;

  const handleFieldChange = (fieldName: string, value: unknown) => {
    setFormValues((prev) => ({ ...prev, [fieldName]: value }));
  };

  const handleListChange = (fieldName: string, draft: string) => {
    setListDrafts((prev) => ({ ...prev, [fieldName]: draft }));
    handleFieldChange(fieldName, draftToList(draft));
  };

  const getListDraft = (fieldName: string): string => {
    if (fieldName in listDrafts) return listDrafts[fieldName];
    return listToDraft(formValues[fieldName]);
  };

  const handleSave = () => {
    if (!isDirty || acting) return;
    // 构造 alias-keyed payload（后端 Config 类同时支持 snake/camel）
    const payload: Record<string, unknown> = {};
    for (const f of schema) {
      payload[f.alias] = formValues[f.name];
    }
    onSave(payload);
  };

  const renderField = (field: ChannelFieldSchema) => {
    const value = formValues[field.name];
    const disabled = acting;
    const { label, description: fieldDesc } = getFieldLabels(channel.name, field);

    const labelEl = (
      <div className="flex items-baseline gap-1.5">
        <span className="text-[11px] font-medium text-muted-foreground/90">
          {label}
          {field.required ? <span className="ml-0.5 text-destructive">*</span> : null}
        </span>
        {fieldDesc ? (
          <span className="text-[10px] text-muted-foreground/60">· {fieldDesc}</span>
        ) : null}
      </div>
    );

    switch (field.ui_type) {
      case "boolean":
        return (
          <div
            key={field.name}
            className="flex items-center justify-between gap-3 rounded-md border bg-background/40 px-3 py-2"
          >
            {labelEl}
            <ToggleSwitch
              checked={Boolean(value)}
              disabled={disabled}
              onClick={() => handleFieldChange(field.name, !value)}
              ariaLabel={field.label}
            />
          </div>
        );

      case "select":
        return (
          <div key={field.name} className="space-y-1">
            {labelEl}
            <select
              value={String(value ?? "")}
              onChange={(e) => handleFieldChange(field.name, e.target.value)}
              disabled={disabled}
              className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {(field.options ?? []).map((opt) => {
                const optLabel = t(
                  `channels.channelFields._options.${field.name}_${opt}`,
                  { defaultValue: opt },
                );
                return (
                  <option key={opt} value={opt}>
                    {optLabel}
                  </option>
                );
              })}
            </select>
          </div>
        );

      case "list":
        return (
          <div key={field.name} className="space-y-1">
            {labelEl}
            <Textarea
              value={getListDraft(field.name)}
              onChange={(e) => handleListChange(field.name, e.target.value)}
              disabled={disabled}
              placeholder={t("channels.listPlaceholder")}
              className="min-h-[60px] resize-y font-mono text-xs"
              spellCheck={false}
            />
          </div>
        );

      case "number":
        return (
          <div key={field.name} className="space-y-1">
            {labelEl}
            <Input
              type="number"
              value={value === null || value === undefined ? "" : String(value)}
              onChange={(e) => {
                const v = e.target.value;
                handleFieldChange(field.name, v === "" ? null : Number(v));
              }}
              disabled={disabled}
              className="h-9 text-sm"
            />
          </div>
        );

      case "password":
        return (
          <div key={field.name} className="space-y-1">
            {labelEl}
            <Input
              type="password"
              value={String(value ?? "")}
              onChange={(e) => handleFieldChange(field.name, e.target.value)}
              disabled={disabled}
              autoComplete="off"
              placeholder={field.secret ? "••••••••" : ""}
              className="h-9 text-sm"
            />
          </div>
        );

      case "text":
      default:
        return (
          <div key={field.name} className="space-y-1">
            {labelEl}
            <Input
              type="text"
              value={String(value ?? "")}
              onChange={(e) => handleFieldChange(field.name, e.target.value)}
              disabled={disabled}
              className="h-9 text-sm"
            />
          </div>
        );
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-sm">
            {displayName}
            <span
              className={cn(
                "inline-flex shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
                channel.is_builtin
                  ? "bg-violet-500/15 text-violet-600 dark:text-violet-300"
                  : "bg-blue-500/15 text-blue-600 dark:text-blue-300",
              )}
            >
              {channel.is_builtin ? t("channels.builtin") : t("channels.custom")}
            </span>
          </DialogTitle>
          {description ? (
            <p className="text-xs text-muted-foreground/80">{description}</p>
          ) : null}
        </DialogHeader>

        <div className="space-y-2">
          {channel.qr_login_supported ? (
            <QrcodeAuthBlock
              channelName={channel.name}
              token={token}
              domain={
                (channel.config?.domain as string | undefined) || "feishu"
              }
              onSuccess={onQrSuccess}
            />
          ) : null}

          {schema.length === 0 ? (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              {t("channels.empty")}
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between gap-2">
                <label className="text-xs font-medium text-muted-foreground">
                  {t("channels.configLabel")}
                </label>
                {!channel.configured ? (
                  <span className="text-[10px] text-muted-foreground/70">
                    {t("channels.defaultHint")}
                  </span>
                ) : null}
              </div>
              <div className="max-h-[60vh] space-y-3 overflow-y-auto px-1">
                {schema.map((field) => renderField(field))}
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="destructive"
            onClick={onDelete}
            disabled={acting || !channel.configured}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {t("channels.delete")}
          </Button>
          <Button
            onClick={handleSave}
            disabled={acting || !isDirty}
          >
            {acting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            {t("channels.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

interface RenameChatDialogProps {
  open: boolean;
  title: string;
  dialogTitle?: string;
  description?: string;
  placeholder?: string;
  onCancel: () => void;
  onConfirm: (title: string) => void;
}

export function RenameChatDialog({
  open,
  title,
  dialogTitle,
  description,
  placeholder,
  onCancel,
  onConfirm,
}: RenameChatDialogProps) {
  const { t } = useTranslation();
  const [value, setValue] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) setValue(title);
  }, [open, title]);

  // Move focus into the rename input each time the dialog opens, replacing
  // the previous raw `autoFocus` (which harms screen-reader / keyboard UX).
  // onOpenAutoFocus fires after Radix Portal content is mounted, so the ref
  // is guaranteed to be set (a plain useEffect would run before the Portal
  // commits and the ref would be null).
  const handleOpenAutoFocus = (event: Event) => {
    event.preventDefault();
    inputRef.current?.focus();
  };

  const trimmed = value.trim();

  return (
    <Dialog open={open} onOpenChange={(next) => {
      if (!next) onCancel();
    }}>
      <DialogContent className="max-w-sm p-5" onOpenAutoFocus={handleOpenAutoFocus}>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!trimmed) return;
            onConfirm(trimmed);
          }}
        >
          <DialogHeader className="text-left">
            <DialogTitle>{dialogTitle ?? t("chat.renameTitle")}</DialogTitle>
            <DialogDescription>
              {description ?? t("chat.renameDescription")}
            </DialogDescription>
          </DialogHeader>
          <Input
            ref={inputRef}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={placeholder ?? t("chat.renamePlaceholder")}
            maxLength={160}
          />
          <DialogFooter className="gap-2 sm:space-x-0">
            <Button type="button" variant="outline" onClick={onCancel}>
              {t("deleteConfirm.cancel")}
            </Button>
            <Button type="submit" disabled={!trimmed}>
              {t("chat.renameSave")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

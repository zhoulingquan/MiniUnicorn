// Image generation section 子 hook: generate_image 工具的 form / dirty / save 逻辑。
//
// 参考 useRuntimeSection.ts 的 Plan & Execute 配置模式。
// form 字段对应后端 image_generation_api 的 update query 参数。
// 凭证完全复用 model_preset, 前端只保留 preset 名 + 图像 API 特有字段。

import { Dispatch, SetStateAction, useCallback, useEffect, useMemo, useState } from "react";

import { updateImageGenerationSettings } from "@/lib/api";
import type { ImageGenerationSettingsUpdate, SettingsPayload } from "@/lib/types";

import type { PendingRestartSections, RestartAwarePayload } from "../types";
import type { UseSectionShared } from "./useWebSearchSection";

/** Image generation section 暴露的状态与回调 */
export interface ImageGenerationSectionState {
  imageGenForm: ImageGenerationSettingsUpdate;
  setImageGenForm: Dispatch<SetStateAction<ImageGenerationSettingsUpdate>>;
  imageGenSaving: boolean;
  imageGenDirty: boolean;
  saveImageGenerationSettings: () => Promise<void>;
}

export function useImageGenerationSection(shared: UseSectionShared): ImageGenerationSectionState {
  const {
    settings,
    token,
    setError,
    applyPayload,
    setPendingRestartSections,
    maybeRestartHostEngine,
  } = shared;

  const [imageGenForm, setImageGenForm] = useState<ImageGenerationSettingsUpdate>({
    enabled: false,
    preset: "default",
    defaultAspectRatio: "1:1",
    defaultImageSize: "1K",
    maxImagesPerTurn: 4,
    saveDir: "generated",
  });
  const [imageGenSaving, setImageGenSaving] = useState(false);

  // 监听 settings 变化同步 form
  useEffect(() => {
    if (!settings) return;
    const ig = settings.image_generation;
    if (!ig) return;
    setImageGenForm({
      enabled: ig.enabled,
      preset: ig.preset,
      defaultAspectRatio: ig.default_aspect_ratio,
      defaultImageSize: ig.default_image_size,
      maxImagesPerTurn: ig.max_images_per_turn,
      saveDir: ig.save_dir,
    });
  }, [settings]);

  const imageGenDirty = useMemo(() => {
    if (!settings?.image_generation) return false;
    const ig = settings.image_generation;
    if (imageGenForm.enabled !== ig.enabled) return true;
    if (imageGenForm.preset !== ig.preset) return true;
    if (imageGenForm.defaultAspectRatio !== ig.default_aspect_ratio) return true;
    if (imageGenForm.defaultImageSize !== ig.default_image_size) return true;
    if (imageGenForm.maxImagesPerTurn !== ig.max_images_per_turn) return true;
    if (imageGenForm.saveDir !== ig.save_dir) return true;
    return false;
  }, [imageGenForm, settings]);

  const saveImageGenerationSettings = useCallback(async () => {
    if (!settings || !imageGenDirty || imageGenSaving) return;
    setImageGenSaving(true);
    try {
      const payload: SettingsPayload = await updateImageGenerationSettings(token, imageGenForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, images: true }));
      }
      await maybeRestartHostEngine(payload as RestartAwarePayload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setImageGenSaving(false);
    }
  }, [
    settings,
    imageGenDirty,
    imageGenSaving,
    imageGenForm,
    token,
    applyPayload,
    setPendingRestartSections,
    maybeRestartHostEngine,
    setError,
  ]);

  return {
    imageGenForm,
    setImageGenForm,
    imageGenSaving,
    imageGenDirty,
    saveImageGenerationSettings,
  };
}

// 导入 PendingRestartSections 类型, 避免 TS "declared but never used" 警告
export type { PendingRestartSections };

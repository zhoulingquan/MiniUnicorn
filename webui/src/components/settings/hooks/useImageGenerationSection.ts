// Image generation section 子 hook: generate_image 工具的 form / dirty / save 逻辑。
//
// 参考 useRuntimeSection.ts 的 Plan & Execute 配置模式。
// form 字段对应后端 image_generation_api 的 update query 参数。
// 凭证完全复用 model_preset, 前端只保留 preset 名 + 图像 API 特有字段。
// save 流程统一走 useSaveAction 原语。

import { Dispatch, SetStateAction, useEffect, useMemo, useState } from "react";

import { updateImageGenerationSettings } from "@/lib/api";
import type { ImageGenerationSettingsUpdate } from "@/lib/types";

import { useSaveAction } from "./useSaveAction";
import type { SaveActionSharedDeps } from "./useSaveAction";
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
  const { settings, token } = shared;

  const [imageGenForm, setImageGenForm] = useState<ImageGenerationSettingsUpdate>({
    enabled: false,
    preset: "default",
    defaultAspectRatio: "1:1",
    defaultImageSize: "1K",
    maxImagesPerTurn: 4,
    saveDir: "generated",
  });

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

  const sharedDeps: SaveActionSharedDeps = {
    applyPayload: shared.applyPayload,
    setError: shared.setError,
    setPendingRestartSections: shared.setPendingRestartSections,
    maybeRestartHostEngine: shared.maybeRestartHostEngine,
  };

  const imageGenAction = useSaveAction<void, ImageGenerationSettingsUpdate>({
    shared: sharedDeps,
    token,
    enabled: !!settings && imageGenDirty,
    buildPayload: () => imageGenForm,
    apiCall: updateImageGenerationSettings,
    restartSectionKey: "images",
  });

  return {
    imageGenForm,
    setImageGenForm,
    imageGenSaving: imageGenAction.saving,
    imageGenDirty,
    saveImageGenerationSettings: imageGenAction.save,
  };
}

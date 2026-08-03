import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InlineAddModelForm } from "@/components/settings/components/InlineAddModelForm";
import { NewModelConfigurationDialog } from "@/components/settings/sections/NewModelConfigurationDialog";
import { RenameChatDialog } from "@/components/RenameChatDialog";
import type { ModelConfigurationDraft } from "@/components/settings/types";

// happy-dom doesn't implement HTMLElement.focus() reliably across renders,
// so we spy on the prototype to observe calls. Each test restores the
// previous implementation to avoid leaking state.
function spyFocus() {
  const focusSpy = vi.spyOn(HTMLElement.prototype, "focus");
  return focusSpy;
}

const baseDraft: ModelConfigurationDraft = {
  label: "",
  provider: "deepseek",
  model: "",
  apiKey: undefined,
  apiBase: undefined,
};

describe("RenameChatDialog focus management", () => {
  it("moves focus into the rename input when the dialog opens", async () => {
    const focusSpy = spyFocus();
    render(
      <RenameChatDialog
        open
        title="Old name"
        onCancel={() => {}}
        onConfirm={() => {}}
      />,
    );

    const input = screen.getByPlaceholderText("Chat name") as HTMLInputElement;
    expect(input).toBeInTheDocument();
    // The input itself should have been the focus target.
    // Radix Dialog mounts content asynchronously, so we wait for the focus call.
    await waitFor(() => {
      const focusCalls = focusSpy.mock.instances as unknown as HTMLElement[];
      expect(focusCalls).toContain(input);
    });
    focusSpy.mockRestore();
  });

  it("does not steal focus while the dialog is closed", () => {
    const focusSpy = spyFocus();
    render(
      <RenameChatDialog
        open={false}
        title="Old name"
        onCancel={() => {}}
        onConfirm={() => {}}
      />,
    );

    const input = screen.queryByPlaceholderText("Chat name") as HTMLInputElement | null;
    expect(input).not.toBeInTheDocument();
    // No focus calls should be issued while the dialog is closed.
    const inputFocusCalls = focusSpy.mock.instances.filter(
      (el) => (el as unknown) instanceof HTMLInputElement,
    );
    expect(inputFocusCalls).toHaveLength(0);
    focusSpy.mockRestore();
  });
});

describe("NewModelConfigurationDialog focus management", () => {
  it("moves focus into the configuration name input when the dialog opens", async () => {
    const focusSpy = spyFocus();
    render(
      <NewModelConfigurationDialog
        open
        draft={baseDraft}
        providers={[{ name: "deepseek", label: "DeepSeek" }]}
        saving={false}
        showProviderLogos={false}
        onOpenChange={() => {}}
        onChangeDraft={() => {}}
        onSave={() => {}}
      />,
    );

    const input = screen.getByPlaceholderText("Fast writing") as HTMLInputElement;
    expect(input).toBeInTheDocument();
    // Radix Dialog mounts content asynchronously, so we wait for the focus call.
    await waitFor(() => {
      const focusCalls = focusSpy.mock.instances as unknown as HTMLElement[];
      expect(focusCalls).toContain(input);
    });
    focusSpy.mockRestore();
  });

  it("does not steal focus while the dialog is closed", () => {
    const focusSpy = spyFocus();
    render(
      <NewModelConfigurationDialog
        open={false}
        draft={baseDraft}
        providers={[{ name: "deepseek", label: "DeepSeek" }]}
        saving={false}
        showProviderLogos={false}
        onOpenChange={() => {}}
        onChangeDraft={() => {}}
        onSave={() => {}}
      />,
    );

    const inputFocusCalls = focusSpy.mock.instances.filter(
      (el) => (el as unknown) instanceof HTMLInputElement,
    );
    expect(inputFocusCalls).toHaveLength(0);
    focusSpy.mockRestore();
  });
});

describe("InlineAddModelForm focus management", () => {
  it("moves focus into the Model ID input on mount", () => {
    const focusSpy = spyFocus();
    render(
      <InlineAddModelForm
        draft={baseDraft}
        fetchedModels={[]}
        modelsLoading={false}
        saving={false}
        isCustom={false}
        onChangeDraft={() => {}}
        onFetchModels={() => {}}
        onSave={() => {}}
        onCancel={() => {}}
      />,
    );

    const input = screen.getByPlaceholderText("e.g. gpt-4o, deepseek-chat") as HTMLInputElement;
    expect(input).toBeInTheDocument();
    const focusCalls = focusSpy.mock.instances as unknown as HTMLElement[];
    expect(focusCalls).toContain(input);
    focusSpy.mockRestore();
  });
});

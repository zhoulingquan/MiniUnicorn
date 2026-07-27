import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { QrcodeAuthBlock } from "@/components/channels/QrcodeAuthBlock";
import type {
  ChannelQrBeginPayload,
  ChannelQrStatusPayload,
} from "@/lib/types";

const beginChannelQrLogin = vi.fn<
  (token: string, name: string, domain?: string) => Promise<ChannelQrBeginPayload>
>();
const pollChannelQrStatus = vi.fn<
  (
    token: string,
    name: string,
    pollToken: string,
    domain?: string,
  ) => Promise<ChannelQrStatusPayload>
>();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    beginChannelQrLogin: (token: string, name: string, domain?: string) =>
      beginChannelQrLogin(token, name, domain),
    pollChannelQrStatus: (
      token: string,
      name: string,
      pollToken: string,
      domain?: string,
    ) => pollChannelQrStatus(token, name, pollToken, domain),
  };
});

function makeBegin(overrides: Partial<ChannelQrBeginPayload> = {}): ChannelQrBeginPayload {
  return {
    qrcode_image: "AAAA",
    scan_url: "https://example.com/qr/old",
    poll_token: "poll-old",
    interval: 5,
    expires_in: 300,
    started_at: Math.floor(Date.now() / 1000),
    ...overrides,
  };
}

beforeEach(() => {
  beginChannelQrLogin.mockReset();
  pollChannelQrStatus.mockReset();
});

describe("QrcodeAuthBlock — generation guard (设计 §4.5)", () => {
  it("a late poll response from the previous begin does not overwrite the new QR state", async () => {
    // First begin returns a "pending" poll that will not resolve until we
    // manually trigger it; meanwhile the user clicks refresh, kicking off a
    // second begin. The first poll's eventual "succeeded" response must NOT
    // mark the block as succeeded.
    const oldBegin = makeBegin({ qrcode_image: "OLD", poll_token: "poll-old" });
    const newBegin = makeBegin({
      qrcode_image: "NEW",
      poll_token: "poll-new",
      scan_url: "https://example.com/qr/new",
    });

    let resolveOldPoll: (r: ChannelQrStatusPayload) => void = () => {};
    beginChannelQrLogin
      .mockResolvedValueOnce(oldBegin)
      .mockResolvedValueOnce(newBegin);
    pollChannelQrStatus
      .mockReturnValueOnce(
        new Promise((r) => {
          resolveOldPoll = r;
        }),
      )
      .mockResolvedValue({ status: "pending" });

    const onSuccess = vi.fn();
    render(
      <QrcodeAuthBlock
        channelName="feishu"
        token="tok"
        onSuccess={onSuccess}
      />,
    );

    // Click "Start QR login" → first begin → old QR shown.
    fireEvent.click(screen.getByRole("button", { name: /Start QR login/i }));
    await waitFor(() =>
      expect(screen.getByRole("img")).toHaveAttribute(
        "src",
        "data:image/png;base64,OLD",
      ),
    );

    // Click "Refresh QR code" → second begin → new QR shown.
    fireEvent.click(screen.getByRole("button", { name: /Refresh QR code/i }));
    await waitFor(() =>
      expect(screen.getByRole("img")).toHaveAttribute(
        "src",
        "data:image/png;base64,NEW",
      ),
    );

    // The old poll's late "succeeded" response arrives after the new begin.
    // Generation guard must drop it — block must NOT transition to success.
    await act(async () => {
      resolveOldPoll({ status: "succeeded", config: { stale: true } });
      await Promise.resolve();
    });

    expect(onSuccess).not.toHaveBeenCalled();
    // Block is still showing the new QR (not the success state).
    expect(screen.getByRole("img")).toHaveAttribute(
      "src",
      "data:image/png;base64,NEW",
    );
    // Success copy is absent.
    expect(screen.queryByText(/Login successful/i)).toBeNull();
  });

  it("a matching-generation poll still transitions to success normally", async () => {
    // Sanity check: when no refresh happens, the success path still works.
    const begin = makeBegin({ qrcode_image: "OK", poll_token: "poll-ok" });
    beginChannelQrLogin.mockResolvedValueOnce(begin);
    pollChannelQrStatus.mockResolvedValueOnce({
      status: "succeeded",
      config: { ok: true },
    });

    const onSuccess = vi.fn();
    render(
      <QrcodeAuthBlock
        channelName="feishu"
        token="tok"
        onSuccess={onSuccess}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Start QR login/i }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ ok: true }));
    expect(screen.getByText(/Login successful/i)).toBeInTheDocument();
  });
});

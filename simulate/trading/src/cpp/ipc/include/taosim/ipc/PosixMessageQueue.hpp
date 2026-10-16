/*
 * SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
 * SPDX-License-Identifier: MIT
 */
#pragma once

extern "C" {
#include <fcntl.h>
#include <mqueue.h>
#include <sys/stat.h>
}

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <functional>
#include <optional>
#include <span>
#include <string>
#include <string_view>

//-------------------------------------------------------------------------

namespace taosim::ipc
{

//-------------------------------------------------------------------------

struct PosixMessageQueueDesc
{
    std::string name;
    int32_t oflag = O_CREAT | O_RDWR;
    mode_t mode = 0666;
    mq_attr attr = {
        .mq_flags = 0,
        .mq_maxmsg = 1,
        .mq_msgsize = sizeof(size_t),
        .mq_curmsgs = 0
    };
    std::optional<size_t> timeout{60'000'000'000};
};

//-------------------------------------------------------------------------

class PosixMessageQueue
{
public:
    explicit PosixMessageQueue(const PosixMessageQueueDesc& desc);
    ~PosixMessageQueue() noexcept;

    [[nodiscard]] mqd_t handle() const noexcept { return m_handle; }
    [[nodiscard]] std::optional<size_t> size() const noexcept;

    bool send(std::span<const char> msg, uint32_t priority = {}) noexcept;
    ptrdiff_t receive(std::span<char> msg, uint32_t* priority = {}) noexcept;
    ptrdiff_t blockingReceive(std::span<char> msg, uint32_t* priority = {}) noexcept;
    ptrdiff_t receiveFor(
        std::span<char> msg, std::chrono::nanoseconds timeout, uint32_t* priority = {}) noexcept;

    [[nodiscard]] std::optional<size_t> timeout() const noexcept { return m_desc.timeout; }

    void flush() noexcept;

    static bool remove(std::string_view name);

private:
    mqd_t m_handle;
    PosixMessageQueueDesc m_desc;
};

//-------------------------------------------------------------------------

// Sends a request on a request/response queue pair after draining both, so a frame left in
// either queue (by a killed engine, or by a validator that answered one) can never pair with
// this request. Call it only before a send, never between a send and its receive.
bool sendFreshRequest(
    PosixMessageQueue& req, PosixMessageQueue& res, std::span<const char> msg) noexcept;

// Sends `request` with sendFreshRequest and waits for the size_t reply, sending it again after every
// full timeout of the response queue without one. The wait is cut into slices and `abandon` is asked
// between them, so a caller that is stopping is not held by a peer that will never answer; the result
// is then nullopt. `onRetry` is told "send" or "receive" whenever the request goes out again.
std::optional<size_t> sendAndAwaitReply(
    PosixMessageQueue& req,
    PosixMessageQueue& res,
    size_t request,
    const std::function<bool()>& abandon,
    const std::function<void(std::string_view)>& onRetry = {},
    std::chrono::nanoseconds slice = std::chrono::milliseconds{200});

//-------------------------------------------------------------------------

}  // namespace taosim::ipc

//-------------------------------------------------------------------------

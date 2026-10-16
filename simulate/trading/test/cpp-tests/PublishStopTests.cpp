// SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
// SPDX-License-Identifier: MIT
//
// The engine publishes a state and waits for the validator's answer, sending the state again after
// each full queue timeout. A stop is acted on only once a step completes, so a validator that has
// already gone away kept the engine in that wait until it was killed, and the shutdown checkpoint was
// never written. The wait now asks between short slices whether to give up. Test-only queue names,
// never the production /taosim-* ones.

#include <gtest/gtest.h>

#include <taosim/ipc/PosixMessageQueue.hpp>

#include <unistd.h>

#include <atomic>
#include <bit>
#include <chrono>
#include <optional>
#include <span>
#include <string>
#include <thread>

using taosim::ipc::PosixMessageQueue;
using taosim::ipc::PosixMessageQueueDesc;
using namespace std::chrono_literals;

namespace
{

std::string testQueueName(const char* role)
{
    return "/taosim-test-publish-stop-" + std::to_string(::getpid()) + "-" + role;
}

PosixMessageQueueDesc testDesc(const std::string& name, std::chrono::nanoseconds timeout)
{
    PosixMessageQueueDesc desc{.name = name};
    desc.timeout = std::optional<size_t>{static_cast<size_t>(timeout.count())};
    return desc;
}

}  // namespace

TEST(PublishStop, AnAnsweredRequestReturnsTheReply)
{
    PosixMessageQueue req{testDesc(testQueueName("req-a"), 2s)};
    PosixMessageQueue res{testDesc(testQueueName("res-a"), 2s)};
    std::thread validator{[&] {
        size_t got{};
        if (req.receive(std::span{std::bit_cast<char*>(&got), sizeof(got)}) != -1) {
            const size_t reply = got + 35;
            res.send(std::span{std::bit_cast<const char*>(&reply), sizeof(reply)});
        }
    }};

    const auto reply = taosim::ipc::sendAndAwaitReply(req, res, 7, [] { return false; });
    validator.join();

    ASSERT_TRUE(reply.has_value());
    EXPECT_EQ(*reply, 42u);
}

TEST(PublishStop, AStopIsHonouredWhileNoValidatorAnswers)
{
    PosixMessageQueue req{testDesc(testQueueName("req-b"), 30s)};
    PosixMessageQueue res{testDesc(testQueueName("res-b"), 30s)};
    std::atomic_bool stop{false};
    std::thread signaller{[&] {
        std::this_thread::sleep_for(300ms);
        stop.store(true);
    }};

    const auto t0 = std::chrono::steady_clock::now();
    const auto reply = taosim::ipc::sendAndAwaitReply(req, res, 7, [&] { return stop.load(); });
    const auto waited = std::chrono::steady_clock::now() - t0;
    signaller.join();

    EXPECT_FALSE(reply.has_value());
    EXPECT_LT(waited, 5s) << "the wait ran on towards the 30 s queue timeout instead of stopping";
}

TEST(PublishStop, ARequestUnansweredForAFullTimeoutIsSentAgain)
{
    PosixMessageQueue req{testDesc(testQueueName("req-c"), 200ms)};
    PosixMessageQueue res{testDesc(testQueueName("res-c"), 200ms)};
    int retries = 0;

    const auto reply = taosim::ipc::sendAndAwaitReply(
        req, res, 7, [&] { return retries >= 2; }, [&](std::string_view) { ++retries; });

    EXPECT_FALSE(reply.has_value());
    EXPECT_GE(retries, 2);
}

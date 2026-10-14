/*
 * SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
 * SPDX-License-Identifier: MIT
 */

// A checkpoint restore keeps the random generator's draw count, so a second resume continues the stream.
//
// The msgpack restore rebuilt RNG{seed} and called the base std::mt19937::discard. That puts the stream back
// at the right position but leaves the draw counter at zero, so the next checkpoint recorded only the draws
// made since the resume. A resume from that checkpoint re-seeded and discarded too few, landed earlier in the
// stream and repeated draws already produced. MagneticField, GBM and JumpDiffusion hold this generator, and
// MagneticField is in the production simulation layouts.

#include <taosim/checkpoint/serialization/process/GBM.hpp>
#include <taosim/checkpoint/serialization/process/RNG.hpp>
#include <taosim/process/GBM.hpp>
#include <taosim/process/RNG.hpp>
#include <taosim/serialization/msgpack/common.hpp>

#include <gtest/gtest.h>

#include <cstdint>
#include <type_traits>
#include <vector>

using taosim::process::GBM;
using taosim::process::GBMDesc;
using taosim::process::RNG;

namespace
{

constexpr uint64_t kSeed = 20261014;

// Pack exactly as a checkpoint does, then restore exactly as a resume does.
template<typename T>
void restoreInto(const T& value, T& restored)
{
    taosim::serialization::BinaryStream stream;
    msgpack::packer<taosim::serialization::BinaryStream> packer{stream};
    packer.pack(value);
    auto handle = msgpack::unpack(stream.data(), stream.size());
    handle.get().convert(restored);
}

std::vector<uint32_t> draw(RNG& rng, int n)
{
    std::vector<uint32_t> out;
    out.reserve(n);
    for (int i = 0; i < n; ++i) {
        out.push_back(rng());
    }
    return out;
}

GBMDesc gbmDesc()
{
    return GBMDesc{.X0 = 100.0, .mu = 0.0, .sigma = 0.2, .dt = 1.0 / 86400.0, .seed = kSeed, .proc = {}};
}

}  // namespace

TEST(RNGCheckpointResume, TwoResumesContinueTheStreamAndTheCount)
{
    constexpr int N = 100, M = 57, K = 200;
    RNG reference{kSeed};
    const auto expected = draw(reference, N + M + K);

    RNG first{kSeed};
    auto got = draw(first, N);
    RNG second;
    restoreInto(first, second);
    EXPECT_EQ(second.callCount(), static_cast<uint64_t>(N)) << "the first resume lost the draw count";
    for (auto v : draw(second, M)) got.push_back(v);
    RNG third;
    restoreInto(second, third);
    EXPECT_EQ(third.callCount(), static_cast<uint64_t>(N + M)) << "the second resume lost the draw count";
    for (auto v : draw(third, K)) got.push_back(v);

    EXPECT_EQ(got, expected) << "the stream repeated or skipped draws across the resumes";
    EXPECT_EQ(third.callCount(), reference.callCount());
}

TEST(RNGCheckpointResume, DiscardKeepsTheCountInStep)
{
    RNG reference{kSeed};
    draw(reference, 10);
    RNG skipped{kSeed};
    skipped.discard(10);
    EXPECT_EQ(skipped.callCount(), 10u);
    EXPECT_EQ(skipped(), reference());
}

TEST(RNGCheckpointResume, TheCountIsSixtyFourBits)
{
    RNG rng{kSeed};
    static_assert(std::is_same_v<std::remove_cvref_t<decltype(rng.callCount())>, uint64_t>,
                  "a 32-bit draw count wraps on a long run and restores the stream at the wrong place");
    SUCCEED();
}

TEST(RNGCheckpointResume, AProcessCheckpointCarriesItsGeneratorThroughTwoResumes)
{
    constexpr int N = 40, M = 25, K = 30;
    RNG reference{kSeed};
    const auto expected = draw(reference, N + M + K);

    GBM first{gbmDesc()};
    auto got = draw(first.state().rng, N);
    GBM second{gbmDesc()};
    restoreInto(first, second);
    for (auto v : draw(second.state().rng, M)) got.push_back(v);
    GBM third{gbmDesc()};
    restoreInto(second, third);
    EXPECT_EQ(third.state().rng.callCount(), static_cast<uint64_t>(N + M))
        << "the process's own checkpoint lost its generator's draw count";
    for (auto v : draw(third.state().rng, K)) got.push_back(v);

    EXPECT_EQ(got, expected) << "the process's generator repeated or skipped draws across the resumes";
}

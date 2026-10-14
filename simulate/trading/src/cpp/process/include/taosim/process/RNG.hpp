/*
 * SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include "CheckpointSerializable.hpp"

#include <random>

//-------------------------------------------------------------------------

namespace taosim::process
{

//-------------------------------------------------------------------------

class RNG : public std::mt19937, public CheckpointSerializable
{
public:
    explicit RNG(uint64_t seed = std::mt19937::default_seed) noexcept;

    std::mt19937::result_type operator()();

    // Skips z draws and counts them, so a checkpoint taken after a skip records the true position.
    void discard(unsigned long long z);

    [[nodiscard]] auto&& callCount(this auto&& self) noexcept { return self.m_callCount; }
    [[nodiscard]] auto&& seedValue(this auto&& self) noexcept { return self.m_seed; }

    // The generator a checkpoint describes: seeded, advanced by callCount draws, and counting from there.
    [[nodiscard]] static RNG restored(uint64_t seed, uint64_t callCount);

    virtual void checkpointSerialize(
        rapidjson::Document& json, const std::string& key = {}) const override;

    [[nodiscard]] static RNG fromCheckpoint(const rapidjson::Value& json);

private:
    uint64_t m_callCount{};
    uint64_t m_seed;
};

//-------------------------------------------------------------------------

}  // namespace taosim::process

//-------------------------------------------------------------------------

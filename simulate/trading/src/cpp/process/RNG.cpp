/*
 * SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
 * SPDX-License-Identifier: MIT
 */
#include <taosim/process/RNG.hpp>

//-------------------------------------------------------------------------

namespace taosim::process
{

//-------------------------------------------------------------------------

RNG::RNG(uint64_t seed) noexcept
    : std::mt19937{seed}, m_seed{seed}
{}

//-------------------------------------------------------------------------

std::mt19937::result_type RNG::operator()()
{
    ++m_callCount;
    return std::mt19937::operator()();
}

//-------------------------------------------------------------------------

void RNG::discard(unsigned long long z)
{
    std::mt19937::discard(z);
    m_callCount += z;
}

//-------------------------------------------------------------------------

RNG RNG::restored(uint64_t seed, uint64_t callCount)
{
    RNG rng{seed};
    rng.discard(callCount);
    return rng;
}

//-------------------------------------------------------------------------

void RNG::checkpointSerialize(rapidjson::Document& json, const std::string& key) const
{
    auto serialize = [this](rapidjson::Document& json) {
        json.SetObject();
        auto& allocator = json.GetAllocator();
        json.AddMember("callCount", rapidjson::Value{m_callCount}, allocator);
        json.AddMember("seed", rapidjson::Value{m_seed}, allocator);
    };
    taosim::json::serializeHelper(json, key, serialize);
}

//-------------------------------------------------------------------------

RNG RNG::fromCheckpoint(const rapidjson::Value& json)
{
    return restored(json["seed"].GetUint64(), json["callCount"].GetUint64());
}

//-------------------------------------------------------------------------

}  // namespace taosim::process

//-------------------------------------------------------------------------
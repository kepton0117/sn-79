/*
 * SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <taosim/matching/DynamicFeePolicy.hpp>
#include <taosim/serialization/msgpack/boost/circular_buffer.hpp>

#include <algorithm>
#include <optional>

//-------------------------------------------------------------------------

namespace msgpack
{

MSGPACK_API_VERSION_NAMESPACE(MSGPACK_DEFAULT_API_NS)
{

namespace adaptor
{

//-------------------------------------------------------------------------

template<>
struct convert<taosim::matching::Volumes>
{
    const msgpack::object& operator()(
        const msgpack::object& o, taosim::matching::Volumes& v) const
    {
        if (o.type != msgpack::type::MAP) {
            throw taosim::serialization::MsgPackError{};
        }

        for (const auto& [k, val] : o.via.map) {
            auto key = k.as<std::string_view>();

            if (key == "aggressive") {
                v.aggressive = val.as<taosim::decimal_t>();
            }
            else if (key == "passive") {
                v.passive = val.as<taosim::decimal_t>();
            }
        }

        return o;
    }
};

template<>
struct pack<taosim::matching::Volumes>
{
    template<typename Stream>
    msgpack::packer<Stream>& operator()(
        msgpack::packer<Stream>& o, const taosim::matching::Volumes& v) const
    {
        o.pack_map(2);

        o.pack("aggressive");
        o.pack(v.aggressive);

        o.pack("passive");
        o.pack(v.passive);

        return o;
    }
};

//-------------------------------------------------------------------------

template<>
struct convert<taosim::matching::DynamicFeePolicy>
{
    const msgpack::object& operator()(
        const msgpack::object& o, taosim::matching::DynamicFeePolicy& v) const
    {
        if (o.type != msgpack::type::MAP) {
            throw taosim::serialization::MsgPackError{};
        }

        std::optional<Timestamp> policyWideLastUpdate;
        bool perBookLastUpdate = false;
        for (const auto& [k, val] : o.via.map) {
            auto key = k.as<std::string_view>();

            if (key == "lastUpdate") {
                policyWideLastUpdate = val.as<Timestamp>();
            }
            else if (key == "lastUpdatePerBook") {
                using T = std::remove_cvref_t<decltype(v.lastUpdate())>;
                v.lastUpdate() = val.as<T>();
                perBookLastUpdate = true;
            }
            else if (key == "totalVolumes") {
                using T = std::remove_cvref_t<decltype(v.totalVolumes())>;
                v.totalVolumes() = val.as<T>();
            }
            else if (key == "totalVolumesPrev") {
                using T = std::remove_cvref_t<decltype(v.totalVolumesPrev())>;
                v.totalVolumesPrev() = val.as<T>();
            }
            else if (key == "volumes") {
                using T = std::remove_cvref_t<decltype(v.volumes())>;
                v.volumes() = val.as<T>();
            }
        }

        // A checkpoint that carries only the policy-wide time gives that time to every book with a history.
        if (!perBookLastUpdate && policyWideLastUpdate.has_value() && *policyWideLastUpdate != 0) {
            v.lastUpdate().clear();
            for (const auto& [bookId, slots] : v.volumes()) {
                v.lastUpdate()[bookId] = *policyWideLastUpdate;
            }
        }

        return o;
    }
};

template<>
struct pack<taosim::matching::DynamicFeePolicy>
{
    template<typename Stream>
    msgpack::packer<Stream>& operator()(
        msgpack::packer<Stream>& o, const taosim::matching::DynamicFeePolicy& v) const
    {
        o.pack_map(6);

        o.pack("type");
        o.pack("dynamic");

        // The latest of the books' times, for a reader that knows only the policy-wide field.
        Timestamp latest = 0;
        for (const auto& [bookId, t] : v.lastUpdate()) {
            latest = std::max(latest, t);
        }
        o.pack("lastUpdate");
        o.pack(latest);

        o.pack("lastUpdatePerBook");
        o.pack(v.lastUpdate());

        o.pack("totalVolumes");
        o.pack(v.totalVolumes());

        o.pack("totalVolumesPrev");
        o.pack(v.totalVolumesPrev());

        o.pack("volumes");
        o.pack(v.volumes());

        return o;
    }
};

//-------------------------------------------------------------------------

}  // namespace adaptor

}  // MSGPACK_API_VERSION_NAMESPACE

}  // namespace msgpack

//-------------------------------------------------------------------------

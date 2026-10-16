// SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
// SPDX-License-Identifier: MIT
//
// A resume puts every log file back to the size the checkpoint recorded for it. The run's own
// archiver packs a window once it closes and removes its files, and a checkpoint taken on the
// window's last instant still lists them, so a file can be gone for a reason that loses nothing:
// its window ended by the checkpoint and the archive holds it whole. A file of a window still
// open at the checkpoint is a different matter, and the restore stops on it.
//
// Files written after the checkpoint belong to the stretch the resume replaces. The file sinks
// append, so they are renamed out of the log pattern before the resumed run opens its files.

#include <gtest/gtest.h>

#include <taosim/checkpoint/CheckpointError.hpp>
#include <taosim/checkpoint/CheckpointManager.hpp>
#include <taosim/checkpoint/helpers.hpp>

#include <msgpack.hpp>
#include <unistd.h>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <regex>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;
using namespace std::chrono_literals;
using taosim::checkpoint::CheckpointManager;

namespace
{

fs::path freshDir(const std::string& tag)
{
    const auto dir = fs::temp_directory_path()
        / ("taosim-test-resume-" + std::to_string(::getpid()) + "-" + tag);
    fs::remove_all(dir);
    fs::create_directories(dir);
    return dir;
}

void writeBytes(const fs::path& path, size_t n)
{
    std::ofstream{path, std::ios::binary} << std::string(n, 'x');
}

using Sizes = std::vector<std::pair<std::string, size_t>>;

void packSizes(msgpack::packer<msgpack::sbuffer>& pk, const Sizes& sizes)
{
    pk.pack_map(static_cast<uint32_t>(sizes.size()));
    for (const auto& [name, size] : sizes) {
        pk.pack(name);
        pk.pack(size);
    }
}

msgpack::object_handle sizesObject(const Sizes& sizes)
{
    msgpack::sbuffer buf;
    msgpack::packer pk{buf};
    packSizes(pk, sizes);
    return msgpack::unpack(buf.data(), buf.size());
}

msgpack::object_handle multiAssetCommon(const std::vector<Sizes>& perRealization)
{
    msgpack::sbuffer buf;
    msgpack::packer pk{buf};
    pk.pack_map(1);
    pk.pack(std::string{"logFileSizes"});
    pk.pack_array(static_cast<uint32_t>(perRealization.size()));
    for (const auto& sizes : perRealization) {
        packSizes(pk, sizes);
    }
    return msgpack::unpack(buf.data(), buf.size());
}

constexpr auto kWindow3to4 = "L2-15.00030000-00040000.log";
constexpr auto kWindow4to5 = "L2-15.00040000-00050000.log";

}  // namespace

TEST(ResumeLogFiles, AListedFileIsCutBackToItsRecordedSize)
{
    const auto dir = freshDir("cut");
    writeBytes(dir / kWindow3to4, 300);
    const auto sizes = sizesObject({{kWindow3to4, 250}});

    taosim::checkpoint::restoreLogFileSizes(sizes.get(), dir, 3h + 50min);

    EXPECT_EQ(fs::file_size(dir / kWindow3to4), 250u);
    fs::remove_all(dir);
}

TEST(ResumeLogFiles, AMissingFileWhoseWindowClosedByTheCheckpointIsLeftToItsArchive)
{
    const auto dir = freshDir("closed");
    const auto sizes = sizesObject({{kWindow3to4, 300}});

    EXPECT_NO_THROW(taosim::checkpoint::restoreLogFileSizes(sizes.get(), dir, 4h));
    EXPECT_FALSE(fs::exists(dir / kWindow3to4)) << "a skipped file must not be recreated empty";
    fs::remove_all(dir);
}

TEST(ResumeLogFiles, AMissingFileOfAWindowStillOpenAtTheCheckpointStopsTheRestoreNamingIt)
{
    const auto dir = freshDir("open");
    const auto sizes = sizesObject({{kWindow3to4, 300}});

    try {
        taosim::checkpoint::restoreLogFileSizes(sizes.get(), dir, 3h + 50min);
        FAIL() << "a missing file of an open window was accepted";
    } catch (const taosim::checkpoint::CheckpointError& e) {
        EXPECT_NE(std::string{e.what()}.find(kWindow3to4), std::string::npos) << e.what();
    }
    fs::remove_all(dir);
}

TEST(ResumeLogFiles, AMissingFileWithNoWindowInItsNameStopsTheRestore)
{
    const auto dir = freshDir("nowindow");
    const auto sizes = sizesObject({{"agents.csv", 10}});

    EXPECT_THROW(
        taosim::checkpoint::restoreLogFileSizes(sizes.get(), dir, 4h),
        taosim::checkpoint::CheckpointError);
    fs::remove_all(dir);
}

TEST(ResumeLogFiles, FilesWrittenAfterTheCheckpointAreRenamedOutOfTheLogPattern)
{
    const auto run = freshDir("later");
    const auto realization = run / "simulation_0-0";
    fs::create_directories(realization);
    fs::create_directories(run / "ckpt");
    writeBytes(realization / kWindow3to4, 300);
    writeBytes(realization / "keep.csv", 9);
    writeBytes(realization / kWindow4to5, 50);
    writeBytes(realization / "fees-15.00040000-00050000.log", 5);
    writeBytes(run / "external_seed.csv", 7);
    const auto common = multiAssetCommon({{{kWindow3to4, 300}, {"keep.csv", 9}}});

    const auto moved = taosim::checkpoint::setAsideLogsWrittenAfter(run, common.get(), 14'400'000'000'000);

    EXPECT_EQ(moved, 2u);
    EXPECT_TRUE(fs::exists(realization / kWindow3to4));
    EXPECT_TRUE(fs::exists(realization / "keep.csv"));
    EXPECT_FALSE(fs::exists(realization / kWindow4to5));
    EXPECT_FALSE(fs::exists(realization / "fees-15.00040000-00050000.log"));
    EXPECT_TRUE(fs::exists(run / "external_seed.csv")) << "the run root is not a realization's log dir";
    size_t setAside = 0;
    for (const auto& entry : fs::directory_iterator{realization}) {
        const auto name = entry.path().filename().string();
        if (name.find("superseded") != std::string::npos) {
            ++setAside;
            EXPECT_FALSE(std::regex_match(name, CheckpointManager::s_relevantLogFilePattern))
                << name << " still matches the log pattern, so the next checkpoint would list it";
        }
    }
    EXPECT_EQ(setAside, 2u);
    fs::remove_all(run);
}

TEST(ResumeLogFiles, ASingleMarketRunSetsAsideAtItsRoot)
{
    const auto run = freshDir("single");
    writeBytes(run / kWindow3to4, 300);
    writeBytes(run / kWindow4to5, 50);
    msgpack::sbuffer buf;
    msgpack::packer pk{buf};
    pk.pack_map(1);
    pk.pack(std::string{"logFileSizes"});
    packSizes(pk, {{kWindow3to4, 300}});
    const auto common = msgpack::unpack(buf.data(), buf.size());

    EXPECT_EQ(taosim::checkpoint::setAsideLogsWrittenAfter(run, common.get(), 14'400'000'000'000), 1u);
    EXPECT_TRUE(fs::exists(run / kWindow3to4));
    EXPECT_FALSE(fs::exists(run / kWindow4to5));
    fs::remove_all(run);
}

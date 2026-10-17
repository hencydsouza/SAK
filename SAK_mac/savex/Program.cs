// savex - Nintendo Switch save data extractor (macOS port helper for SAK).
//
// Replaces SAK's Windows call:
//   hactoolnet -k prod.keys -t save <savefile> --outdir <tmpdir>
// using the LibHac NuGet package (pinned 0.19.0), which is the library
// hactoolnet itself is built on.
//
// Output: hactoolnet-style "Label: value" lines, then a final single line
//   JSON: {"title_id":...,"user_id":...,"save_id":...,"save_type":...,"timestamp":...}
//
// Exit codes: 0 ok, 1 usage error, 2 keys problem, 3 invalid/unreadable save file,
//             4 extraction (copy-out) failure.

using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using LibHac.Common;
using LibHac.Common.Keys;
using LibHac.Fs;
using LibHac.FsSystem;
using LibHac.Tools.FsSystem;
using LibHac.Tools.FsSystem.Save;

namespace SaveX;

internal static class Program
{
    private const int ColLen = 36; // hactoolnet pads labels to a fixed column

    private const string Usage =
        "usage: savex -k <prod.keys> [--outdir <dir>] <savefile>\n" +
        "\n" +
        "Reads a Nintendo Switch save data file (e.g. from the USER partition's\n" +
        "save/ folder), prints its header metadata, and optionally extracts its\n" +
        "contents.\n" +
        "\n" +
        "options:\n" +
        "  -k, --keys <file>     prod.keys file (required)\n" +
        "  -o, --outdir <dir>    extract all files from the save into <dir>\n" +
        "  -h, --help            show this help\n" +
        "\n" +
        "output: hactoolnet-style lines (Title ID:, User ID:, Save ID:, Save Type:,\n" +
        "Timestamp:, ...) followed by a final line 'JSON: {...}'.\n" +
        "\n" +
        "exit codes: 0 ok, 1 usage, 2 keys error, 3 invalid save file, 4 extract error";

    private static int Main(string[] args)
    {
        string? keysPath = null, outDir = null, savePath = null;

        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            switch (a)
            {
                case "-h":
                case "--help":
                    Console.WriteLine(Usage);
                    return 0;
                case "-k":
                case "--keys":
                case "--keyset":
                    if (++i >= args.Length) return UsageError($"missing value for {a}");
                    keysPath = args[i];
                    break;
                case "-o":
                case "--outdir":
                    if (++i >= args.Length) return UsageError($"missing value for {a}");
                    outDir = args[i];
                    break;
                default:
                    if (a.StartsWith('-') && a.Length > 1) return UsageError($"unknown option '{a}'");
                    if (savePath != null) return UsageError($"unexpected extra argument '{a}'");
                    savePath = a;
                    break;
            }
        }

        if (keysPath == null) return UsageError("missing -k <prod.keys>");
        if (savePath == null) return UsageError("missing <savefile>");

        // ---- keys ----
        if (!File.Exists(keysPath)) return Fail(2, $"keys file not found: {keysPath}");
        KeySet keySet;
        try
        {
            if (new FileInfo(keysPath).Length == 0) return Fail(2, $"keys file is empty: {keysPath}");
            keySet = new KeySet();
            ExternalKeyReader.ReadKeyFile(keySet, keysPath, null, null, null);
        }
        catch (Exception e)
        {
            return Fail(2, $"failed to read keys file '{keysPath}': {e.Message}");
        }

        // ---- save file sanity checks (before handing to LibHac) ----
        if (Directory.Exists(savePath)) return Fail(3, $"'{savePath}' is a directory, expected a save data file");
        if (!File.Exists(savePath)) return Fail(3, $"save file not found: {savePath}");
        string? magicError = CheckSaveMagic(savePath);
        if (magicError != null) return Fail(3, $"invalid save file '{savePath}': {magicError}");

        // ---- open + mount ----
        LocalStorage? storage = null;
        SaveDataFileSystem? save = null;
        try
        {
            try
            {
                storage = new LocalStorage(savePath, FileAccess.Read);
                save = new SaveDataFileSystem(keySet, storage, IntegrityCheckLevel.None, true);
            }
            catch (Exception e)
            {
                return Fail(3, $"invalid save file '{savePath}': LibHac could not mount it as a save filesystem ({Describe(e)})");
            }

            Header header = save.Header;
            ExtraData extra = header.ExtraData;

            string titleIdLower = extra.TitleId.ToString("x16");
            string saveIdLower = extra.SaveId.ToString("x16");
            string ownerIdLower = extra.SaveOwnerId.ToString("x16");
            string userIdGuid = extra.UserId.ToString();          // e.g. 01234567-89ab-cdef-0123-456789abcdef
            string userIdNoDash = extra.UserId.ToString("N");     // same, dashes removed
            string userIdRaw = ReadRawUserId(header) ?? Convert.ToHexString(extra.UserId.ToByteArray());
            string saveType = extra.Type.ToString();
            string timestamp = FormatTimestamp(extra.Timestamp);

            // hactoolnet-style block (labels mirror hactoolnet's ProcessSave header print)
            var sb = new StringBuilder();
            sb.AppendLine("Savefile:");
            PrintItem(sb, "CMAC Signature:", header.SignatureValidity.ToString());
            PrintItem(sb, "Title ID:", titleIdLower);
            PrintItem(sb, "User ID:", userIdGuid);
            PrintItem(sb, "Save ID:", saveIdLower);
            PrintItem(sb, "Save Type:", saveType);
            PrintItem(sb, "Owner ID:", ownerIdLower);
            PrintItem(sb, "Timestamp:", timestamp);
            PrintItem(sb, "Save Data Size:", $"0x{extra.DataSize:x16}");
            PrintItem(sb, "Journal Size:", $"0x{extra.JournalSize:x16}");
            PrintItem(sb, "Header Hash:", header.HeaderHashValidity.ToString());
            Console.Write(sb.ToString());

            if (header.SignatureValidity != Validity.Valid)
            {
                Console.Error.WriteLine(
                    $"savex: note: CMAC signature is {header.SignatureValidity} " +
                    "(normal if prod.keys lacks this console's save_mac_key; contents are still readable)");
            }

            int rc = 0;
            int fileCount = 0;
            if (outDir != null)
            {
                try
                {
                    Directory.CreateDirectory(outDir);
                    fileCount = CountFiles(save);
                    save.Extract(outDir, null);
                    Console.WriteLine($"Extracted {fileCount} file(s) to {System.IO.Path.GetFullPath(outDir)}");
                }
                catch (Exception e)
                {
                    Console.Error.WriteLine($"savex: error: extraction to '{outDir}' failed: {Describe(e)}");
                    rc = 4;
                }
            }

            var json = new Dictionary<string, object?>
            {
                ["title_id"] = extra.TitleId.ToString("X16"),
                ["user_id"] = userIdNoDash,
                ["user_id_raw"] = userIdRaw,
                ["save_id"] = extra.SaveId.ToString("X16"),
                ["save_type"] = saveType,
                ["timestamp"] = timestamp,
                ["timestamp_unix"] = extra.Timestamp,
                ["owner_id"] = extra.SaveOwnerId.ToString("X16"),
                ["cmac"] = header.SignatureValidity.ToString(),
                ["extracted"] = outDir != null && rc == 0,
                ["file_count"] = outDir != null ? fileCount : null,
            };
            Console.WriteLine("JSON: " + JsonSerializer.Serialize(json));
            return rc;
        }
        catch (Exception e)
        {
            return Fail(3, $"failed to read save file '{savePath}': {Describe(e)}");
        }
        finally
        {
            save?.Dispose();
            storage?.Dispose();
        }
    }

    // hactoolnet: PrintItem(sb, colLen, prefix, value) => prefix padded to colLen, then value.
    private static void PrintItem(StringBuilder sb, string prefix, string value)
    {
        sb.Append(prefix.PadRight(ColLen)).AppendLine(value);
    }

    // hactoolnet prints DateTimeOffset.FromUnixTimeSeconds(ts) as "yyyy-MM-dd HH:mm:ss" (UTC).
    private static string FormatTimestamp(long unixSeconds)
    {
        try
        {
            return DateTimeOffset.FromUnixTimeSeconds(unixSeconds).ToString("yyyy-MM-dd HH:mm:ss",
                System.Globalization.CultureInfo.InvariantCulture);
        }
        catch (ArgumentOutOfRangeException)
        {
            return unixSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
        }
    }

    // A Switch save image starts with a 0x10-byte CMAC, then padding, then the
    // "DISF" FS-layout header at 0x100. Reject obvious non-saves early.
    private static string? CheckSaveMagic(string path)
    {
        try
        {
            using var fs = File.OpenRead(path);
            if (fs.Length < 0x4000) return $"file too small ({fs.Length} bytes) to be a save data image";
            fs.Position = 0x100;
            Span<byte> magic = stackalloc byte[4];
            if (fs.Read(magic) != 4) return "could not read header";
            if (!magic.SequenceEqual("DISF"u8))
                return $"missing 'DISF' magic at 0x100 (found {Convert.ToHexString(magic)})";
            return null;
        }
        catch (Exception e)
        {
            return $"cannot read file: {e.Message}";
        }
    }

    // SaveDataExtraData layout: 0x00 ProgramId (u64), 0x08 UserId (16 bytes), 0x18 save id ...
    // Returns the user id in on-disk byte order as upper hex.
    private static string? ReadRawUserId(Header header)
    {
        try
        {
            byte[] buf = new byte[0x18];
            if (header.ExtraDataStorage.Read(0, buf).IsFailure()) return null;
            // sanity: first u64 must match the parsed title id, otherwise layout assumption is wrong
            if (BinaryPrimitives.ReadUInt64LittleEndian(buf) != header.ExtraData.TitleId) return null;
            return Convert.ToHexString(buf, 8, 16);
        }
        catch
        {
            return null;
        }
    }

    private static int CountFiles(SaveDataFileSystem save)
    {
        int n = 0;
        foreach (var e in save.EnumerateEntries("/", "*", SearchOptions.RecurseSubdirectories))
            if (e.Type == DirectoryEntryType.File) n++;
        return n;
    }

    private static string Describe(Exception e) =>
        e is HorizonResultException hr ? hr.Message : $"{e.GetType().Name}: {e.Message}";

    private static int UsageError(string msg)
    {
        Console.Error.WriteLine($"savex: error: {msg}");
        Console.Error.WriteLine("usage: savex -k <prod.keys> [--outdir <dir>] <savefile>   (see --help)");
        return 1;
    }

    private static int Fail(int code, string msg)
    {
        Console.Error.WriteLine($"savex: error: {msg}");
        return code;
    }
}

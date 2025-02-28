import argparse
import os
import shutil


UTT_PREFIX = "csd"
DEV_LIST = ["046"]
TEST_LIST = ["047", "048", "049", "050"]


def train_check(song):
    return not test_check(song) and not dev_check(song)


def dev_check(song):
    for dev in DEV_LIST:
        if dev in song:
            return True
    return False


def test_check(song):
    for test in TEST_LIST:
        if test in song:
            return True
    return False


def pack_zero(string, size=20):
    if len(string) < size:
        string = "0" * (size - len(string)) + string
    return string


def makedir(data_url):
    if os.path.exists(data_url):
        shutil.rmtree(data_url)

    os.makedirs(data_url)


def process_text_info(text):
    info = open(text, "r", encoding="utf-8")
    label_info = []
    text_info = []
    for line in info.readlines():
        line = line.strip().split()
        if line[0] == "start":
            continue
        label_info.append(
            "{} {} {}".format(float(line[0]), float(line[1]), line[3].strip())
        )
        text_info.append(line[3].strip())
    return " ".join(label_info), " ".join(text_info)


def process_subset(src_data, subset, check_func):
    subfolder = os.listdir(src_data)
    makedir(subset)
    wavscp = open(os.path.join(subset, "wav.scp"), "w", encoding="utf-8")
    utt2spk = open(os.path.join(subset, "utt2spk"), "w", encoding="utf-8")
    midiscp = open(os.path.join(subset, "midi.scp"), "w", encoding="utf-8")
    text_scp = open(os.path.join(subset, "text"), "w", encoding="utf-8")
    label_scp = open(os.path.join(subset, "label"), "w", encoding="utf-8")

    for csv in os.listdir(src_data, "csv"):

        if not os.path.isfile(os.path.join(src_data, "csv", csv)):
            continue
        if not check_func(folder):
            continue
        song_name = csv[:-4]
        utt_id = "{}_{}".format(UTT_PREFIX, pack_zero(song_name))
        wavscp.write(
            "{} sox -t wavpcm {} -c 1 -t wavpcm -b 16 -|\n".format(
                utt_id, os.path.join(src_data, "wav", "{}.wav".format(song_name))
            )
        )
        utt2spk.write("{} {}\n".format(utt_id, UTT_PREFIX))
        label_info, text_info = process_text_info(
            os.path.join(src_data, "csv", "{}.csv".format(song_name))
        )
        text_scp.write("{} {}\n".format(utt_id, text_info))
        label_scp.write("{} {}\n".format(utt_id, label_info))
        midiscp.write(
            "{} {}\n".format(
                utt_id, os.path.join(src_data, "mid", "{}.mid".format(song_name))
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare Data for Oniku Database")
    parser.add_argument("src_data", type=str, help="source data directory")
    parser.add_argument("train", type=str, help="train set")
    parser.add_argument("dev", type=str, help="development set")
    parser.add_argument("test", type=str, help="test set")
    args = parser.parse_args()

    process_subset(args.src_data, args.train, train_check)
    process_subset(args.src_data, args.dev, dev_check)
    process_subset(args.src_data, args.test, test_check)

import argparse
import html
import os
import re
import shutil
import sys
from collections import OrderedDict, defaultdict
from configparser import RawConfigParser
from copy import deepcopy
from datetime import datetime
from glob import glob
from importlib import import_module
from json import dumps
from time import strptime, time, strftime
from typing import Dict, List, Tuple, Any, Union

from PIL import Image
from jinja2 import Environment, FileSystemLoader
from markdown2 import Markdown
from pytz import timezone

import utils
from build_rss_feed import build_rss_feed

VERSION = "0.3.8"

AUTOGENERATE_WARNING = """<!--
!! DO NOT EDIT THIS FILE !!
It is auto-generated and any work you do here will be replaced the next time this page is generated.
If you want to edit any of these files, follow the instructions at https://github.com/ryanvilbrandt/comic_git/wiki/Extra-Features#editing-existing-pages
-->
"""
BASE_DIRECTORY = ""
MARKDOWN = Markdown(extras=["strike", "break-on-newline"])


def web_path(rel_path: str):
    if rel_path.startswith("/"):
        return BASE_DIRECTORY + rel_path
    return rel_path


def delete_output_file_space(comic_info: RawConfigParser = None):
    shutil.rmtree("comic", ignore_errors=True)
    if os.path.isfile("feed.xml"):
        os.remove("feed.xml")
    if comic_info is None:
        comic_info = read_info("comic_info.ini")
    for page in get_pages_list(comic_info):
        if page["template_name"] == "index":
            if os.path.exists("index.html"):
                os.remove("index.html")
        elif page["template_name"] == "404":
            if os.path.exists("404.html"):
                os.remove("404.html")
        else:
            if os.path.exists(page["template_name"]):
                shutil.rmtree(page["template_name"])
    for comic in get_extra_comics_list(comic_info):
        if os.path.exists(comic):
            shutil.rmtree(comic)


def setup_output_file_space(comic_info: RawConfigParser):
    # Clean workspace, i.e. delete old files
    delete_output_file_space(comic_info)


def read_info(filepath, to_dict=False):
    with open(filepath, "rb") as f:
        info_string = f.read().decode("utf-8")
    if not re.search(r"^\[.*?]", info_string):
        # print(filepath + " has no section")
        info_string = "[DEFAULT]\n" + info_string
    info = RawConfigParser()
    info.optionxform = str
    info.read_string(info_string)
    if to_dict:
        # TODO: Support multiple sections
        if not list(info.keys()) == ["DEFAULT"]:
            raise NotImplementedError("Configs with multiple sections not yet supported")
        return dict(info["DEFAULT"])
    return info


def get_option(comic_info: RawConfigParser, section: str, option: str, option_type: type=str, default: Any=None) \
        -> Union[str, int, float, bool]:
    if comic_info.has_section(section):
        if comic_info.has_option(section, option):
            if option_type == str:
                return comic_info.get(section, option)
            elif option_type == int:
                return comic_info.getint(section, option)
            elif option_type == float:
                return comic_info.getfloat(section, option)
            elif option_type == bool:
                return comic_info.getboolean(section, option)
            else:
                raise ValueError(f"Invalid option type: {option_type}")
    return default


def get_links_list(comic_info: RawConfigParser):
    link_list = []
    for option in comic_info.options("Links Bar"):
        link_list.append({"name": option, "url": web_path(comic_info.get("Links Bar", option))})
    return link_list


def get_pages_list(comic_info: RawConfigParser, section_name="Pages"):
    if comic_info.has_section("Pages"):
        return [{"template_name": option, "title": web_path(comic_info.get(section_name, option))}
                for option in comic_info.options(section_name)]
    return []


def get_extra_comics_list(comic_info: RawConfigParser) -> List[str]:
    if comic_info.has_option("Comic Settings", "Extra comics"):
        return utils.str_to_list(comic_info.get("Comic Settings", "Extra comics"))
    return []


def run_hook(theme: str, func: str, args: List[Any]) -> Any:
    """
    Determines if the hooks.py file has been added to the given theme, and if that file contains the given function.
    If so, it will call that function with the given args.
    :param theme: Name of the theme to check in for the hooks.py file
    :param func: Function name to call
    :param args: Args list to pass to the function
    :return: The return value of the function called, if one was found. Otherwise, None.
    """
    if os.path.exists(f"{CONTENT_DIR}/themes/{theme}/scripts/hooks.py"):
        current_path = os.path.abspath(".")
        if current_path not in sys.path:
            sys.path.append(current_path)
            print(f"Path updated: {sys.path}")
        hooks = import_module(f"{CONTENT_DIR}.themes.{theme}.scripts.hooks")
        if hasattr(hooks, func):
            method = getattr(hooks, func)
            return method(*args)
    return None


def build_and_publish_comic_pages(comic_url: str, comic_folder: str, comic_info: RawConfigParser,
                                  delete_scheduled_posts: bool, publish_all_comics: bool, processing_times: list):
    page_info_list, scheduled_post_count = get_page_info_list(
        comic_folder, comic_info, delete_scheduled_posts, publish_all_comics
    )
    print([p["page_name"] for p in page_info_list])
    processing_times.append((f"Get info for all pages in '{comic_folder}'", time()))

    # Save page_info_list.json file for use by other pages
    save_page_info_json_file(comic_folder, page_info_list, scheduled_post_count)
    processing_times.append((f"Save page_info_list.json file in '{comic_folder}'", time()))

    # Build full comic data dicts, to build templates with
    comic_data_dicts = build_comic_data_dicts(comic_folder, comic_info, page_info_list)
    processing_times.append((f"Build full comic data dicts for '{comic_folder}'", time()))

    # Create low-res and thumbnail versions of all the comic pages
    process_comic_images(comic_info, comic_data_dicts)
    processing_times.append((f"Process comic images in '{comic_folder}'", time()))

    # Load home page text
    if os.path.isfile(f"{CONTENT_DIR}/{comic_folder}home page.txt"):
        with open(f"{CONTENT_DIR}/{comic_folder}home page.txt") as f:
            home_page_text = f.read()
    else:
        home_page_text = ""

    # Write page info to comic HTML pages
    show_uncategorized = get_option(comic_info, "Archive", "Show Uncategorized comics", option_type=bool, default=True)
    global_values = {
        "autogenerate_warning": AUTOGENERATE_WARNING,
        "version": VERSION,
        "comic_title": comic_info.get("Comic Info", "Comic name"),
        "comic_author": comic_info.get("Comic Info", "Author"),
        "comic_description": comic_info.get("Comic Info", "Description"),
        "banner_image": web_path(
            get_option(comic_info, "Comic Settings", "Banner image", default=f"/{CONTENT_DIR}/images/banner.png")
        ),
        "theme": get_option(comic_info, "Comic Settings", "Theme", default="default"),
        "comic_url": comic_url,
        "base_dir": BASE_DIRECTORY,
        "comic_base_dir": f"{BASE_DIRECTORY}/{comic_folder}".rstrip("/"),  # e.g. /base_dir/extra_comic
        "content_base_dir": f"{BASE_DIRECTORY}/{CONTENT_DIR}/{comic_folder}".rstrip("/"),  # e.g. /base_dir/content/extra_comic
        "links": get_links_list(comic_info),
        "use_images_in_navigation_bar": comic_info.getboolean("Comic Settings", "Use images in navigation bar"),
        "use_thumbnails": comic_info.getboolean("Archive", "Use thumbnails"),
        "storylines": get_storylines(comic_data_dicts, show_uncategorized),
        "home_page_text": home_page_text,
        "google_analytics_id": get_option(comic_info, "Google Analytics", "Tracking ID", default=""),
        "scheduled_post_count": scheduled_post_count,
    }
    # Update the global values with any custom values returned by the hook.py file's extra_global_value's function
    extra_global_variables = run_hook(
        global_values["theme"],
        "extra_global_values",
        [comic_folder, comic_info, comic_data_dicts]
    )
    if extra_global_variables:
        global_values.update(extra_global_variables)
    write_html_files(comic_folder, comic_info, comic_data_dicts, global_values)
    processing_times.append((f"Write HTML files for '{comic_folder}'", time()))
    return comic_data_dicts


def get_page_info_list(comic_folder: str, comic_info: RawConfigParser, delete_scheduled_posts: bool,
                       publish_all_comics: bool) -> Tuple[List[Dict], int]:
    date_format = comic_info.get("Comic Settings", "Date format")
    tz_info = timezone(comic_info.get("Comic Settings", "Timezone"))
    local_time = datetime.now(tz=tz_info)
    print(f"Local time is {local_time}")
    page_info_list = []
    scheduled_post_count = 0
    auto_detect_comic_images = get_option(
        comic_info, "Comic Settings", "Auto-detect comic images", option_type=bool, default=False
    )
    theme = get_option(comic_info, "Comic Settings", "Theme", default="default")
    for page_path in glob(f"{CONTENT_DIR}/{comic_folder}comics/*/"):
        filepath = f"{page_path}info.ini"
        if not os.path.exists(f"{page_path}info.ini"):
            print(f"{page_path} is missing its info.ini file. Skipping")
            continue
        page_info = read_info(filepath, to_dict=True)
        post_date = tz_info.localize(datetime.strptime(page_info["Post date"], date_format))
        if post_date > local_time and not publish_all_comics:
            scheduled_post_count += 1
            # Post date is in the future, so delete the folder with the resources
            if delete_scheduled_posts:
                print(f"Deleting {page_path}")
                shutil.rmtree(page_path)
        else:
            if not page_info.get("Filename", ""):
                if not auto_detect_comic_images:
                    raise FileNotFoundError(f"Comic image filename must be provided in {page_path}info.ini")
                image_files = []
                for filename in os.listdir(page_path):
                    if filename == "thumbnail.jpg":
                        continue
                    if re.search(r"\.(jpg|jpeg|png|tif|tiff|gif|bmp|webp|webv|svg|eps)$", filename):
                        image_files.append(filename)
                if len(image_files) != 1:
                    raise FileNotFoundError(
                        f"Found {len(image_files)} images when attempting to auto-detect image files in {page_path}. "
                        f"({image_files}) When using the 'Auto-detect comic images' option, you must not have any "
                        f"image file in your comic folder other than your comic page and your archive thumbnail "
                        f"(thumbnail.jpg)."
                    )
                page_info["Filename"] = image_files[0]
            page_info["page_name"] = os.path.basename(os.path.normpath(page_path))
            page_info["Storyline"] = page_info.get("Storyline", "")
            page_info["Characters"] = utils.str_to_list(page_info.get("Characters", ""))
            page_info["Tags"] = utils.str_to_list(page_info.get("Tags", ""))
            hook_result = run_hook(theme, "extra_page_info_processing",
                                   [comic_folder, comic_info, page_path, page_info])
            if hook_result:
                page_info = hook_result
            print(page_info)
            page_info_list.append(page_info)

    page_info_list = sorted(
        page_info_list,
        key=lambda x: (strptime(x["Post date"], date_format), x["page_name"])
    )
    return page_info_list, scheduled_post_count


def save_page_info_json_file(comic_folder: str, page_info_list: List, scheduled_post_count: int):
    d = {
        "page_info_list": page_info_list,
        "scheduled_post_count": scheduled_post_count
    }
    os.makedirs(f"{comic_folder}comic", exist_ok=True)
    with open(f"{comic_folder}comic/page_info_list.json", "w") as f:
        f.write(dumps(d))


def get_ids(comic_list: List[Dict], index):
    return {
        "first_id": comic_list[0]["page_name"],
        "previous_id": comic_list[max(0, index - 1)]["page_name"],
        "current_id": comic_list[index]["page_name"],
        "next_id": comic_list[min(len(comic_list) - 1, index + 1)]["page_name"],
        "last_id": comic_list[-1]["page_name"]
    }


def get_transcripts(comic_folder: str, comic_info: RawConfigParser, page_name: str) -> OrderedDict:
    if not comic_info.getboolean("Transcripts", "Enable transcripts"):
        return OrderedDict()
    transcripts = OrderedDict()
    if get_option(comic_info, "Transcripts", "Load transcripts from comic folder", option_type=bool, default=True):
        load_transcripts_from_folder(transcripts, f"{CONTENT_DIR}/{comic_folder}comics", page_name)
    transcripts_dir = get_option(comic_info, "Transcripts", "Transcripts folder", default=f"")
    if transcripts_dir:
        load_transcripts_from_folder(transcripts, transcripts_dir, page_name)
    default_language = get_option(comic_info, "Transcripts", "Default language", default=f"English")
    if default_language in transcripts:
        transcripts.move_to_end(default_language, last=False)
    return transcripts


def load_transcripts_from_folder(transcripts: OrderedDict, transcripts_dir: str, page_name: str):
    for transcript_path in sorted(glob(os.path.join(transcripts_dir, page_name, "*.txt"))):
        if transcript_path.endswith("post.txt"):
            continue
        language = os.path.splitext(os.path.basename(transcript_path))[0]
        with open(transcript_path, "rb") as f:
            transcripts[language] = MARKDOWN.convert(f.read().decode("utf-8"))


def create_comic_data(comic_folder: str, comic_info: RawConfigParser, page_info: dict,
                      first_id: str, previous_id: str, current_id: str, next_id: str, last_id: str):
    print("Building page {}...".format(page_info["page_name"]))
    page_dir = f"{CONTENT_DIR}/{comic_folder}comics/{page_info['page_name']}/"
    archive_post_date = strftime(comic_info.get("Archive", "Date format"),
                                 strptime(page_info["Post date"], comic_info.get("Comic Settings", "Date format")))
    post_html = []
    post_text_paths = [
        f"{CONTENT_DIR}/{comic_folder}before post text.txt",
        f"{CONTENT_DIR}/{comic_folder}before post text.html",
        page_dir + "post.txt",
        f"{CONTENT_DIR}/{comic_folder}after post text.txt",
        f"{CONTENT_DIR}/{comic_folder}after post text.html",
    ]
    for post_text_path in post_text_paths:
        if os.path.exists(post_text_path):
            with open(post_text_path, "rb") as f:
                post_html.append(f.read().decode("utf-8"))
    post_html = MARKDOWN.convert("\n\n".join(post_html))
    d = {
        "page_name": page_info["page_name"],
        "filename": page_info["Filename"],
        "comic_path": page_dir + page_info["Filename"],
        "thumbnail_path": os.path.join(page_dir, "thumbnail.jpg"),
        "alt_text": html.escape(page_info["Alt text"]),
        "first_id": first_id,
        "previous_id": previous_id,
        "current_id": current_id,
        "next_id": next_id,
        "last_id": last_id,
        "page_title": page_info["Title"],
        "post_date": page_info["Post date"],
        "archive_post_date": archive_post_date,
        "storyline": None if "Storyline" not in page_info else page_info["Storyline"],
        "characters": page_info["Characters"],
        "tags": page_info["Tags"],
        "post_html": post_html,
        "transcripts": get_transcripts(comic_folder, comic_info, page_info["page_name"]),
    }
    theme = get_option(comic_info, "Comic Settings", "Theme", default="default")
    hook_result = run_hook(theme, "extra_comic_dict_processing", [comic_folder, comic_info, d])
    if hook_result:
        d = hook_result
    print(d)
    return d


def build_comic_data_dicts(comic_folder: str, comic_info: RawConfigParser, page_info_list: List[Dict]) -> List[Dict]:
    return [
        create_comic_data(comic_folder, comic_info, page_info, **get_ids(page_info_list, i))
        for i, page_info in enumerate(page_info_list)
    ]


def resize(im, size):
    im_w, im_h = im.size
    if "," in size:
        # Convert a string of the form "100, 36" into a 2-tuple of ints (100, 36)
        w, h = size.strip().split(",")
        w, h = w.strip(), h.strip()
    elif size.endswith("%"):
        # Convert a percentage (50%) into a new size (50, 18)
        size = float(size.strip().strip("%"))
        size = size / 100
        w, h = im_w * size, im_h * size
    elif size.endswith("h"):
        # Scale to set height, and adjust width to keep aspect ratio
        h = int(size[:-1].strip())
        w = im_w / im_h * h
    elif size.endswith("w"):
        # Scale to set width, and adjust height to keep aspect ratio
        w = int(size[:-1].strip())
        h = im_h / im_w * w
    else:
        raise ValueError("Unknown resize value: {!r}".format(size))
    return im.resize((int(w), int(h)))


def save_image(im, path):
    try:
        # If saving as JPEG, force convert to RGB first
        if path.lower().endswith("jpg") or path.lower().endswith("jpeg"):
            if im.mode != 'RGB':
                im = im.convert('RGB')
        im.save(path)
    except OSError as e:
        if str(e) == "cannot write mode RGBA as JPEG":
            # Get rid of transparency
            bg = Image.new("RGB", im.size, "WHITE")
            bg.paste(im, (0, 0), im)
            bg.save(path)
        else:
            raise


def process_comic_image(comic_info, comic_page_path):
    section = "Image Reprocessing"
    comic_page_dir = os.path.dirname(comic_page_path)
    comic_page_name, comic_page_ext = os.path.splitext(os.path.basename(comic_page_path))
    with open(comic_page_path, "rb") as f:
        im = Image.open(f)
        thumbnail_path = os.path.join(comic_page_dir, "thumbnail.jpg")
        if comic_info.getboolean(section, "Overwrite existing images") or not os.path.isfile(thumbnail_path):
            print(f"Creating thumbnail for {comic_page_name}")
            thumb_im = resize(im, comic_info.get(section, "Thumbnail size"))
            save_image(thumb_im, thumbnail_path)


def process_comic_images(comic_info: RawConfigParser, comic_data_dicts: List[Dict]):
    section = "Image Reprocessing"
    if comic_info.getboolean(section, "Create thumbnails"):
        for comic_data in comic_data_dicts:
            process_comic_image(comic_info, comic_data["comic_path"])


def get_storylines(comic_data_dicts: List[Dict], show_uncategorized: bool) -> OrderedDict:
    # Start with an OrderedDict, so we can easily drop the pages we encounter in the proper buckets, while keeping
    # their proper order
    storylines_dict = OrderedDict()
    for comic_data in comic_data_dicts:
        storyline = comic_data["storyline"]
        if not storyline:
            if not show_uncategorized:
                continue
            storyline = "Uncategorized"
        if storyline not in storylines_dict.keys():
            storylines_dict[storyline] = []
        storylines_dict[storyline].append(comic_data.copy())
    if "Uncategorized" in storylines_dict:
        storylines_dict.move_to_end("Uncategorized")
    return storylines_dict


def write_html_files(comic_folder: str, comic_info: RawConfigParser, comic_data_dicts: List[Dict], global_values: Dict):
    # Load Jinja environment
    template_folders = ["src/templates"]
    theme = get_option(comic_info, "Comic Settings", "Theme", default="default")
    if theme:
        template_folders.insert(0, f"{CONTENT_DIR}/themes/{theme}/templates")
    print(f"Template folders: {template_folders}")
    utils.jinja_environment = Environment(loader=FileSystemLoader(template_folders))
    # Write individual comic pages
    print("Writing {} comic pages...".format(len(comic_data_dicts)))
    for comic_data_dict in comic_data_dicts:
        html_path = f"{comic_folder}comic/{comic_data_dict['page_name']}/index.html"
        comic_data_dict.update(global_values)
        utils.write_to_template("comic", html_path, comic_data_dict)
    write_other_pages(comic_folder, comic_info, comic_data_dicts, global_values)
    run_hook(global_values["theme"], "build_other_pages", [comic_folder, comic_info, comic_data_dicts])


def write_other_pages(comic_folder: str, comic_info: RawConfigParser, comic_data_dicts: List[Dict],
                      global_values: Dict):
    last_comic_page = comic_data_dicts[-1] if comic_data_dicts else {}
    pages_list = get_pages_list(comic_info)
    for page in pages_list:
        if page["template_name"] == "tagged":
            write_tagged_pages(comic_data_dicts, global_values)
            continue
        if page["template_name"].lower() in ("index", "404"):
            html_path = f"{page['template_name']}.html"
        else:
            html_path = os.path.join(page['template_name'], "index.html")
        if comic_folder:
            html_path = os.path.join(comic_folder, html_path)
        # Don't build latest page if there are no comics published
        if page["template_name"] == "latest" and not comic_data_dicts:
            continue
        data_dict = {}
        data_dict.update(last_comic_page)
        if page["title"]:
            data_dict["page_title"] = page["title"]
        data_dict.update(global_values)
        utils.write_to_template(page["template_name"], html_path, data_dict)


def write_tagged_pages(comic_data_dicts: List[Dict], global_values: Dict):
    if not comic_data_dicts:
        return
    tags = defaultdict(list)
    for page in comic_data_dicts:
        for character in page.get("characters", []):
            tags[character].append(page)
        for tag in page.get("tags", []):
            tags[tag].append(page)
    for tag, pages in tags.items():
        data_dict = {
            "tag": tag,
            "tagged_pages": pages
        }
        data_dict.update(global_values)
        utils.write_to_template("tagged", f"tagged/{tag}/index.html", data_dict)


def get_extra_comic_info(folder_name: str, comic_info: RawConfigParser):
    comic_info = deepcopy(comic_info)
    # Always delete existing Pages section; by default, extra comic provides no additional pages
    del comic_info["Pages"]
    # Delete "Links Bar" from original if the extra comic's info has that section defined
    extra_comic_info = RawConfigParser()
    extra_comic_info.read(f"{CONTENT_DIR}/{folder_name}/comic_info.ini")
    if extra_comic_info.has_section("Links Bar"):
        del comic_info["Links Bar"]
    # Read the extra comic info in again, to merge with the original comic info
    comic_info.read(f"{CONTENT_DIR}/{folder_name}/comic_info.ini")
    return comic_info


def print_processing_times(processing_times: List[Tuple[str, float]]):
    last_processed_time = None
    print("")
    for name, t in processing_times:
        if last_processed_time is not None:
            print("{}: {:.2f} ms".format(name, (t - last_processed_time) * 1000))
        last_processed_time = t
    print("{}: {:.2f} ms".format("Total time", (processing_times[-1][1] - processing_times[0][1]) * 1000))


def main(delete_scheduled_posts=False, publish_all_comics=False):
    global BASE_DIRECTORY
    global CONTENT_DIR
    processing_times = [("Start", time())]

    # Get site-wide settings for this comic
    comic_info = read_info("comic_info.ini")
    comic_url, BASE_DIRECTORY = utils.get_comic_url(comic_info)
    theme = get_option(comic_info, "Comic Settings", "Theme", default="default")
    CONTENT_DIR = get_option(comic_info, "Comic Settings", "Content folder", default="content")
    utils.find_project_root(CONTENT_DIR)

    processing_times.append(("Get comic settings", time()))

    run_hook(theme, "preprocess", [comic_info])

    processing_times.append(("Preprocessing hook", time()))

    # Setup output file space
    setup_output_file_space(comic_info)
    processing_times.append(("Setup output file space", time()))

    # Build and publish pages for main comic
    print("Main comic")
    comic_data_dicts = build_and_publish_comic_pages(
        comic_url, "", comic_info, delete_scheduled_posts, publish_all_comics, processing_times
    )

    # Build RSS feed
    build_rss_feed(comic_info, comic_data_dicts)
    processing_times.append(("Build RSS feed", time()))

    # Build any extra comics that may be needed
    for extra_comic in get_extra_comics_list(comic_info):
        print(extra_comic)
        extra_comic_info = get_extra_comic_info(extra_comic, comic_info)
        os.makedirs(extra_comic, exist_ok=True)
        build_and_publish_comic_pages(
            comic_url, extra_comic.strip("/") + "/", extra_comic_info, delete_scheduled_posts, publish_all_comics,
            processing_times
        )

    run_hook(theme, "postprocess", [comic_info])

    processing_times.append(("Postprocessing hook", time()))

    print_processing_times(processing_times)


def parse_args():
    parser = argparse.ArgumentParser(description='Manual build of comic_git')
    parser.add_argument(
        "-d",
        "--delete-scheduled-posts",
        action="store_true",
        help="Deletes scheduled post content when the script is run. USE AT YOUR OWN RISK! You can discard your "
             "changes in GitHub Desktop if you accidentally delete important files."
    )
    parser.add_argument(
        "-p",
        "--publish-all-posts",
        action="store_true",
        help="Will publish all comics, even ones with a publish date set in the future."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(args.delete_scheduled_posts, args.publish_all_posts)

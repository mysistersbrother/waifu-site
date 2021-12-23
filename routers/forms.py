import asyncio

import asyncpg

import quart
from routers.utils import (
    requires_authorization,
    permissions_check,
    fetch_user_safe,
    TooHighResolution,
)
from quart import Blueprint, render_template, request, current_app

import io
import os
import xxhash

from PIL import Image as ImagePIL
from colorthief import ColorThief
import webcolors

blueprint = Blueprint(
    "forms", __name__, template_folder="static/html", url_prefix="/forms"
)
VERIFIED_UPLOADERS = [508346978288271360]
ALLOWED_EXTENSION = [".png", ".webp", ".gif", ".jpg", ".jpeg"]


def allowed_file(filename):
    ext = os.path.splitext(filename)[1].lower()
    status = ext in ALLOWED_EXTENSION
    if status:
        return ext


def check_res(content, is_gif):
    with ImagePIL.open(io.BytesIO(content)) as img:
        wid, hgt = img.size
    if wid > 7500 or hgt > 7500:
        raise TooHighResolution
    elif (wid > 2000 or hgt > 2000) and is_gif:
        raise TooHighResolution
    return (wid >= 800 and hgt >= 1000) or (wid >= 400 and hgt >= 200 and is_gif)


def get_dominant(fileobj):
    color_thief = ColorThief(fileobj)
    return webcolors.rgb_to_hex(color_thief.get_color())


async def insert_db(
    fileobj, filename_no_ext, filename, extension, source, tags, loop, user=None
):
    dominant_color = await loop.run_in_executor(None, get_dominant, fileobj)
    fileobj.seek(0)
    async with current_app.pool.acquire() as conn:
        async with conn.transaction():

            if user:
                ur = False if user.id in VERIFIED_UPLOADERS else True
                await conn.execute(
                    "INSERT INTO Registered_user(id,name) VALUES($1,$2) ON CONFLICT (id) DO UPDATE SET name=$2",
                    user.id,
                    str(user),
                )

                await conn.execute(
                    "INSERT INTO Images (file,extension,dominant_color,source,under_review,uploader) VALUES($1,$2,$3,$4,$5,$6)",
                    filename_no_ext,
                    extension,
                    str(dominant_color),
                    source,
                    ur,
                    user.id,
                )
            else:
                await conn.execute(
                    "INSERT INTO Images (file,extension,dominant_color,source,under_review) VALUES($1,$2,$3,$4,$5)",
                    filename_no_ext,
                    extension,
                    str(dominant_color),
                    source,
                    True,
                )
            for d in tags:
                await conn.execute(
                    "INSERT  INTO LinkedTags (image,tag_id) VALUES($1,$2)",
                    filename_no_ext,
                    d,
                )
            async with current_app.boto3session.client(
                "s3",
                region_name=current_app.config["s3zone"],
                endpoint_url=current_app.config["s3endpoint"],
            ) as s3:
                await s3.upload_fileobj(
                    fileobj,
                    current_app.config["s3bucket"],
                    filename,
                    ExtraArgs={
                        "ContentType": f'image/{extension.replace(".","")}',
                        "ACL": "public-read",
                    },
                )


@blueprint.route("/upload/", methods=["POST"])
async def form_upload():
    loop = asyncio.get_event_loop()
    forms = await request.form
    tags = forms.getlist("tags[]")
    source = forms.get("source")
    files = await request.files
    file = files.get("file")
    if not (file and tags):
        return (
            dict(
                message="Sorry, the server did not received all the data it needed. Please retry."
            ),
            400,
        )
    extension = allowed_file(file.filename)
    if not extension:
        return (
            dict(
                message="Sorry your file extension isn't allowed Please retry with another file."
            ),
            400,
        )
    content = file.read()
    file.seek(0)
    filename_no_ext = xxhash.xxh3_64_hexdigest(content)
    filename = filename_no_ext + extension
    if not source or len(source) < 4:
        source = None
    try:
        rtres = await loop.run_in_executor(
            None, check_res, content, True if extension == ".gif" else False
        )
    except TooHighResolution:
        return dict(message="Sorry, Your image has a too high resolution."), 400
    if not rtres:
        return (
            dict(
                message='Sorry, Your image has a too low resolution, Please consider using images enlarger such as <a href="http://waifu2x.udp.jp/index.fr.html">waifu2x</a>.'
            ),
            400,
        )
    image_preview = f"{quart.url_for('general.preview_')}?image={filename}"
    try:
        if await current_app.discord.authorized:
            user = await fetch_user_safe()
            await insert_db(
                file,
                filename_no_ext,
                filename,
                extension,
                source,
                tags,
                loop,
                user=user,
            )
        else:
            await insert_db(
                file, filename_no_ext, filename, extension, source, tags, loop
            )
    except asyncpg.exceptions.UniqueViolationError:
        return (
            dict(
                message=f'Sorry this picture already exist, you can find it <a href="{image_preview}">here</a>.'
            ),
            409,
        )
    return dict(message=image_preview)


@blueprint.route("/manage/", methods=["POST"])
@requires_authorization
@permissions_check("manage_images")
async def forms_manage():
    image = request.args.get("image")
    image_no_ext = os.path.splitext(image)[0]
    filename_no_ext = None
    dominant_color = None
    extension = None
    loop = asyncio.get_event_loop()
    forms = await request.form
    tags = forms.getlist("tags")
    source = forms.get("source")
    files = await request.files
    file = files.get("file")
    if not source or len(source) < 4:
        source = None
    if file:
        extension = allowed_file(file.filename)
        if not extension:
            return (
                await render_template(
                    "upload_error.html",
                    message="Sorry your file extension isn't allowed Please retry with another file.",
                ),
                400,
            )
        content = file.read()

        filename_no_ext = xxhash.xxh3_64_hexdigest(content)
        filename = filename_no_ext + extension
        if not await loop.run_in_executor(
            None, check_res, content, True if extension == ".gif" else False
        ):
            return (
                await render_template(
                    "upload_error.html",
                    message='Sorry, Your image has a too low resolution, Please consider using images enlarger such as <a href="http://waifu2x.udp.jp/index.fr.html">waifu2x</a>.',
                ),
                400,
            )
        dominant_color = await loop.run_in_executor(None, get_dominant, file)
        file.seek(0)

    try:
        async with current_app.pool.acquire() as conn:
            async with conn.transaction():
                temp_filename = filename_no_ext if file else image_no_ext
                await conn.execute(
                    "UPDATE Images SET source=$1,file=COALESCE($2,file),extension=COALESCE($3,extension),dominant_color=COALESCE($4,dominant_color) WHERE file=$5",
                    source if source else None,
                    filename_no_ext,
                    extension,
                    dominant_color,
                    image_no_ext,
                )
                await conn.execute(
                    "DELETE FROM LinkedTags WHERE image=$1", temp_filename
                )
                for d in tags:
                    await conn.execute(
                        "INSERT INTO LinkedTags (image,tag_id) VALUES($1,$2)",
                        temp_filename,
                        int(d),
                    )

                async with current_app.boto3session.client(
                    "s3",
                    region_name=current_app.config["s3zone"],
                    endpoint_url=current_app.config["s3endpoint"],
                ) as s3:
                    if file:
                        await s3.delete_object(
                            Bucket=current_app.config["s3bucket"], Key=image
                        )
                        await s3.upload_fileobj(
                            file,
                            current_app.config["s3bucket"],
                            filename,
                            ExtraArgs={
                                "ContentType": f'image/{extension.replace(".","")}',
                                "ACL": "public-read",
                            },
                        )
    except asyncpg.exceptions.UniqueViolationError:
        file = None

    return quart.redirect(
        quart.url_for("general.manage_") + f"?image={filename if file else image}"
    )
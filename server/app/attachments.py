from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app import storage
from app.auth import AUTH_ERROR, Db, Owner
from app.contracts import AttachmentView, ErrorDetail
from app.models import Attachment, Entry
from app.projects import NOT_FOUND

router = APIRouter(prefix="/v1", tags=["attachments"], responses=AUTH_ERROR)
UPLOAD_PATH = "/v1/attachments"
Sha256 = Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")]


@router.post(
    "/attachments",
    status_code=201,
    responses={
        200: {
            "model": AttachmentView,
            "description": "Existing upload for this submission ID.",
        },
        409: {
            "model": ErrorDetail,
            "description": "The submission ID already has a different file.",
        },
        411: {"model": ErrorDetail, "description": "Content-Length is required."},
        413: {"model": ErrorDetail, "description": "The file exceeds 10 MiB."},
        415: {
            "model": ErrorDetail,
            "description": "Only JPEG, PNG, HEIC and WebP images are accepted.",
        },
        422: {
            "model": ErrorDetail,
            "description": "The file does not match the declared SHA-256.",
        },
    },
)
def upload_attachment(
    owner: Owner,
    db: Db,
    response: Response,
    client_submission_id: Annotated[UUID, Form()],
    sha256: Sha256,
    file: Annotated[UploadFile, File()],
) -> AttachmentView:
    """Store one image privately before the entry that uses it is created.

    Use the entry's client_submission_id. Retrying the same file returns the
    original upload. Unlinked uploads are removed after 24 hours.
    """
    try:
        tmp, size, digest, head = storage.receive(file.file)
    except storage.TooLarge:
        raise HTTPException(413, "attachment exceeds 10 MiB") from None
    try:
        if digest != sha256:
            raise HTTPException(422, "attachment checksum mismatch")
        content_type = storage.sniff_image(head)
        if content_type is None:
            raise HTTPException(415, "unsupported image type")
        attachment = db.scalar(
            insert(Attachment)
            .values(
                owner_id=owner.id,
                client_submission_id=client_submission_id,
                content_type=content_type,
                size=size,
                sha256=digest,
            )
            .on_conflict_do_nothing(constraint="uq_attachments_submission")
            .returning(Attachment)
        )
        if attachment is None:
            existing = db.scalars(
                select(Attachment).where(
                    Attachment.owner_id == owner.id,
                    Attachment.client_submission_id == client_submission_id,
                )
            ).one()
            if existing.sha256 != digest:
                raise HTTPException(
                    409, "submission already has a different attachment"
                )
            response.status_code = 200
            return AttachmentView.model_validate(existing)
        storage.commit(tmp, owner.id, attachment.id)
        try:
            db.commit()
        except BaseException:
            storage.discard(owner.id, attachment.id)
            raise
        return AttachmentView.model_validate(attachment)
    finally:
        tmp.unlink(missing_ok=True)


@router.get(
    "/attachments/{attachment_id}",
    response_class=FileResponse,
    responses={**NOT_FOUND, 200: {"content": {"image/*": {}}}},
)
def read_attachment(attachment_id: UUID, owner: Owner, db: Db) -> FileResponse:
    attachment = db.scalar(
        select(Attachment)
        .outerjoin(
            Entry,
            (Entry.owner_id == Attachment.owner_id) & (Entry.id == Attachment.entry_id),
        )
        .where(
            Attachment.owner_id == owner.id,
            Attachment.id == attachment_id,
            Entry.deleted_at.is_(None),
        )
    )
    path = storage.attachment_path(owner.id, attachment_id)
    if attachment is None or not path.is_file():
        raise HTTPException(404, "attachment not found")
    return FileResponse(path, media_type=attachment.content_type)

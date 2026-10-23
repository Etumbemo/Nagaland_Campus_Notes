from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field


def validate_password(value: str) -> str:
    if not any(character.isupper() for character in value):
        raise ValueError("Password must contain at least one uppercase letter")
    if not any(not character.isalnum() and not character.isspace() for character in value):
        raise ValueError("Password must contain at least one special symbol")
    return value


Password = Annotated[
    str,
    Field(min_length=8, max_length=128),
    AfterValidator(validate_password),
]


def validate_phone(value: str) -> str:
    phone = value.strip()
    if not phone.isascii() or not phone.isdigit() or len(phone) != 10:
        raise ValueError("Phone number must contain exactly 10 digits")
    return phone


Phone = Annotated[
    str,
    Field(min_length=10, max_length=10),
    AfterValidator(validate_phone),
]


def validate_required_text(value: str) -> str:
    value = value.strip()
    if len(value) < 2:
        raise ValueError("Must contain at least 2 non-space characters")
    if not any(character.isalnum() for character in value):
        raise ValueError("Must contain at least one letter or number")
    return value


RegistrationText = Annotated[
    str,
    Field(min_length=2),
    AfterValidator(validate_required_text),
]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginOtpConfirm(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")


class LoginOtpResend(LoginRequest):
    pass


class PasswordResetRequest(BaseModel):
    email: EmailStr


class EmailVerificationRequest(BaseModel):
    email: EmailStr


class EmailVerificationConfirm(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")


class PasswordResetConfirm(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")
    new_password: Password


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    name: Annotated[RegistrationText, Field(max_length=160)]
    account_type: Literal["user"] = "user"


class InstituteRegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    name: Annotated[RegistrationText, Field(max_length=160)]
    phone: Phone
    designation: Annotated[RegistrationText, Field(max_length=120)]
    institute_name: Annotated[RegistrationText, Field(max_length=200)]


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str = Field(min_length=1, max_length=32)


class DepartmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, min_length=1, max_length=32)


class PaperCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    subject_code: str | None = Field(default=None, max_length=80)
    department_id: str
    semester: int = Field(ge=1, le=12)
    exam_type: str = Field(min_length=1, max_length=80)
    # The AI pipeline only understands a bare four-digit year (parse_exam_year).
    # Accepting "2024-25" here stored a paper that no AI query could ever select.
    year: str = Field(pattern=r"^\d{4}$")
    description: str | None = None
    file_url: str | None = Field(default=None, max_length=500)


class PaperUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    subject_code: str | None = Field(default=None, max_length=80)
    department_id: str | None = None
    semester: int | None = Field(default=None, ge=1, le=12)
    exam_type: str | None = Field(default=None, min_length=1, max_length=80)
    year: str | None = Field(default=None, pattern=r"^\d{4}$")
    description: str | None = None
    file_url: str | None = Field(default=None, max_length=500)


class AIPreparationGenerateRequest(BaseModel):
    feature: Literal["mock_test", "important_questions"]
    subject: str = Field(min_length=1, max_length=200)
    subject_code: str | None = Field(default=None, max_length=80)
    department_id: str = Field(min_length=1, max_length=36)
    semester: int = Field(ge=1, le=12)
    start_year: int = Field(ge=1900, le=2100)
    end_year: int = Field(ge=1900, le=2100)
    question_count: int = Field(ge=1, le=30)
    hosted_fallback_consent: bool = False


class UserCreate(BaseModel):
    email: EmailStr
    password: Password
    name: str = Field(min_length=1, max_length=160)
    username: str | None = Field(default=None, max_length=80)
    role: Literal["super_admin"] = "super_admin"
    department_id: str | None = None


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    email: EmailStr | None = None
    username: str | None = Field(default=None, max_length=80)
    institute_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    department_id: str | None = None
    password: Password | None = None


class InstituteApprovalUpdate(BaseModel):
    approval_status: Literal["approved", "rejected"]


class ResourceReviewUpdate(BaseModel):
    status: Literal["approved", "rejected"]


class ResourceUploadPermissionUpdate(BaseModel):
    status: Literal["approved", "rejected"]

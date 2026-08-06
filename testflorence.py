import torch
from PIL import Image

from transformers import (
    AutoProcessor,
    AutoModelForCausalLM
)


MODEL_NAME = "microsoft/Florence-2-base-ft"


print("=" * 60)
print("Florence-2 Test")
print("=" * 60)


# --------------------------------------------------
# Device
# --------------------------------------------------

if torch.cuda.is_available():

    device = "cuda"
    dtype = torch.float16

else:

    device = "cpu"
    dtype = torch.float32


print("Device:", device)
print("Torch:", torch.__version__)


# --------------------------------------------------
# Load Processor
# --------------------------------------------------

print()
print("Loading processor...")

processor = AutoProcessor.from_pretrained(
    MODEL_NAME,
    trust_remote_code=True
)

print("Processor loaded.")


# --------------------------------------------------
# Load Model
# --------------------------------------------------

print()
print("Loading Florence-2 model...")

model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    torch_dtype=dtype,
    trust_remote_code=True
)

model = model.to(
    device=device,
    dtype=dtype
)

print("Model loaded.")


# --------------------------------------------------
# Load Image
# --------------------------------------------------

image_path = "input/test.jpg"

print()
print("Opening:", image_path)

image = Image.open(image_path).convert("RGB")

print("Image size:", image.size)


# --------------------------------------------------
# Florence task
# --------------------------------------------------

task = "<DETAILED_CAPTION>"

print()
print("Running task:", task)


inputs = processor(
    text=task,
    images=image,
    return_tensors="pt"
)

inputs = {
    key: value.to(
        device=device,
        dtype=dtype if value.is_floating_point() else value.dtype
    )
    for key, value in inputs.items()
}


# --------------------------------------------------
# Generate
# --------------------------------------------------

generated_ids = model.generate(
    input_ids=inputs["input_ids"],
    pixel_values=inputs["pixel_values"],
    max_new_tokens=512,
    num_beams=3
)


# --------------------------------------------------
# Decode
# --------------------------------------------------

generated_text = processor.batch_decode(
    generated_ids,
    skip_special_tokens=False
)[0]


print()
print("=" * 60)
print("RAW RESULT")
print("=" * 60)

print(generated_text)


# --------------------------------------------------
# Post process
# --------------------------------------------------

result = processor.post_process_generation(
    generated_text,
    task=task,
    image_size=image.size
)


print()
print("=" * 60)
print("PROCESSED RESULT")
print("=" * 60)

print(result)

print()
print("SUCCESS")
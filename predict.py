# Prediction interface for Cog ⚙️
# https://github.com/replicate/cog/blob/main/docs/python.md

from cog import BasePredictor, Input, Path
import os
import re
import time
import torch
import tarfile
import tempfile
import subprocess
import numpy as np
from typing import List
from diffusers import FluxPipeline
from transformers import CLIPImageProcessor
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker
)

MODEL_CACHE = "checkpoints"
MODEL_URL = "https://weights.replicate.delivery/default/black-forest-labs/FLUX.1-dev/model-cache.tar"
SAFETY_CACHE = "safety-cache"
FEATURE_EXTRACTOR = "feature-extractor"
SAFETY_URL = "https://weights.replicate.delivery/default/sdxl/safety-1.0.tar"

def download_weights(url, dest, file=False):
    start = time.time()
    print("downloading url: ", url)
    print("downloading to: ", dest)
    if not file:
        subprocess.check_call(["pget", "-x", url, dest], close_fds=False)
    else:
        subprocess.check_call(["pget", url, dest], close_fds=False)
    print("downloading took: ", time.time() - start)

def extract_tar(tar_path, extract_path):
    with tarfile.open(tar_path, 'r') as tar:
        tar.extractall(path=extract_path)
    return extract_path

class Predictor(BasePredictor):
    def setup(self) -> None:
        """Load the model into memory to make running multiple predictions efficient"""
        start = time.time()
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

        print("Loading safety checker...")
        if not os.path.exists(SAFETY_CACHE):
            download_weights(SAFETY_URL, SAFETY_CACHE)
        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            SAFETY_CACHE, torch_dtype=torch.float16
        ).to("cuda")
        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACTOR)
        
        print("Loading Flux txt2img Pipeline")
        if not os.path.exists(MODEL_CACHE):
            download_weights(MODEL_URL, MODEL_CACHE)
        self.txt2img_pipe = FluxPipeline.from_pretrained(
            "black-forest-labs/FLUX.1-dev",
            torch_dtype=torch.bfloat16,
            cache_dir=MODEL_CACHE
        ).to("cuda")
        print("setup took: ", time.time() - start)

    @torch.amp.autocast('cuda')
    def run_safety_checker(self, image):
        safety_checker_input = self.feature_extractor(image, return_tensors="pt").to("cuda")
        np_image = [np.array(val) for val in image]
        image, has_nsfw_concept = self.safety_checker(
            images=np_image,
            clip_input=safety_checker_input.pixel_values.to(torch.float16),
        )
        return image, has_nsfw_concept

    def aspect_ratio_to_width_height(self, aspect_ratio: str):
        aspect_ratios = {
            "1:1": (1024, 1024),"16:9": (1344, 768),"21:9": (1536, 640),
            "3:2": (1216, 832),"2:3": (832, 1216),"4:5": (896, 1088),
            "5:4": (1088, 896),"9:16": (768, 1344),"9:21": (640, 1536),
        }
        return aspect_ratios.get(aspect_ratio)

    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(description="Prompt for generated image"),
        aspect_ratio: str = Input(
            description="Aspect ratio for the generated image",
            choices=["1:1", "16:9", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21"],
            default="1:1"),
        num_outputs: int = Input(
            description="Number of images to output.",
            ge=1,
            le=4,
            default=1,
        ),
        num_inference_steps: int = Input(
            description="Number of inference steps",
            ge=1,le=50,default=28,
        ),
        guidance_scale: float = Input(
            description="Guidance scale for the diffusion process",
            ge=0,le=10,default=3.5,
        ),
        seed: int = Input(description="Random seed. Set for reproducible generation", default=None),
        output_format: str = Input(
            description="Format of the output images",
            choices=["webp", "jpg", "png"],
            default="webp",
        ),
        output_quality: int = Input(
            description="Quality when saving the output images, from 0 to 100. 100 is best quality, 0 is lowest quality. Not relevant for .png outputs",
            default=80,
            ge=0,
            le=100,
        ),
        hf_lora: str = Input(
            description="Huggingface path, or URL to the LoRA weights. Ex: alvdansen/frosting_lane_flux",
            default=None,
        ),
        lora_scale: float = Input(
            description="Scale for the LoRA weights",
            ge=0,le=1, default=0.8,
        ),
        disable_safety_checker: bool = Input(
            description="Disable safety checker for generated images. This feature is only available through the API. See [https://replicate.com/docs/how-does-replicate-work#safety](https://replicate.com/docs/how-does-replicate-work#safety)",
            default=False,
        ),
    ) -> List[Path]:
        """Run a single prediction on the model"""
        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        width, height = self.aspect_ratio_to_width_height(aspect_ratio)
        max_sequence_length=512
        
        flux_kwargs = {}
        print(f"Prompt: {prompt}")
        print("txt2img mode")
        flux_kwargs["width"] = width
        flux_kwargs["height"] = height
        pipe = self.txt2img_pipe

        if hf_lora is not None:
            joint_attention_kwargs={"scale": lora_scale}
            flux_kwargs["joint_attention_kwargs"] = joint_attention_kwargs
            if re.match(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$", hf_lora):
                print(f"Loading LoRA weights from HF path:{hf_lora}")
                self.txt2img_pipe.load_lora_weights(hf_lora)
            elif re.match(r"^https?://huggingface.co", hf_lora):
                print(f"Downloading LoRA weights from HF URL: {hf_lora}")
                huggingface_slug = re.search(r"^https?://huggingface.co/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)", hf_lora).group(1)
                print(f"HuggingFace slug from URL: {huggingface_slug}")
                weight_name = hf_lora.split('/')[-1]
                print(f"Weight name from URL: {weight_name}")
                self.txt2img_pipe.load_lora_weights(huggingface_slug, weight_name=weight_name)
            elif re.match(r"^https?://.*\.(safetensors|tar)$", hf_lora) or re.match(r"^https?://replicate.delivery/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/trained_model.tar", hf_lora):
                file_extension = 'tar' if hf_lora.endswith('.tar') or 'replicate.delivery' in hf_lora else 'safetensors'
                lora_path = f"/tmp/lora.{file_extension}"
                if os.path.exists(lora_path):
                    os.remove(lora_path)
                print(f"Downloading LoRA weights from URL: {hf_lora}")
                download_weights(hf_lora, lora_path, file=True)
                
                if file_extension == 'tar':
                    with tempfile.TemporaryDirectory() as tmpdir:
                        extract_tar(lora_path, tmpdir)
                        safetensors_files = []
                        for root, dirs, files in os.walk(tmpdir):
                            safetensors_files.extend([os.path.join(root, f) for f in files if f.endswith('.safetensors')])
                        if safetensors_files:
                            print(f"Found {len(safetensors_files)} .safetensors file(s)")
                            safetensors_file = safetensors_files[0]
                            self.txt2img_pipe.load_lora_weights(safetensors_file)
                        else:
                            raise Exception("No .safetensors file found in the tar archive.")
                else:  # safetensors
                    self.txt2img_pipe.load_lora_weights(lora_path)
            else:
                raise Exception(f"Invalid parameter for hf_lora, must be a HuggingFace path/URL, or URL to a .safetensors/.tar file")
        else:
            flux_kwargs["joint_attention_kwargs"] = None
            pipe.unload_lora_weights()

        generator = torch.Generator("cuda").manual_seed(seed)

        def image_generator():
            for i in range(num_outputs):
                yield pipe(
                    prompt=prompt,
                    guidance_scale=guidance_scale,
                    generator=generator,
                    num_inference_steps=num_inference_steps,
                    max_sequence_length=max_sequence_length,
                    output_type="pil",
                    **flux_kwargs
                ).images[0]

        safe_images_count = 0
        for i, image in enumerate(image_generator()):
            if not disable_safety_checker:
                _, has_nsfw_content = self.run_safety_checker([image])
                if has_nsfw_content[0]:
                    print(f"NSFW content detected in image {i}")
                    continue

            output_path = f"/tmp/out-{safe_images_count}.{output_format}"
            if output_format != 'png':
                image.save(output_path, quality=output_quality, optimize=True)
            else:
                image.save(output_path)
            
            safe_images_count += 1
            yield Path(output_path)

        # Unload LoRA weights after all images are generated
        if hf_lora is not None:
            self.txt2img_pipe.unload_lora_weights()

        if safe_images_count == 0:
            raise Exception("NSFW content detected in all images. Try running it again, or try a different prompt.")
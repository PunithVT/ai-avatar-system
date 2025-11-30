import boto3
from botocore.exceptions import ClientError
import aioboto3
import logging
from typing import Optional, BinaryIO
import io
from app.config import settings

logger = logging.getLogger(__name__)


class StorageService:
    """AWS S3 Storage Service"""
    
    def __init__(self):
        self.bucket_name = settings.S3_BUCKET_NAME
        self.region = settings.AWS_REGION
        self.cloudfront_domain = settings.CLOUDFRONT_DOMAIN
        
        # Synchronous client
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=self.region
        )
        
        # Async session
        self.session = aioboto3.Session(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=self.region
        )
    
    async def initialize(self):
        """Initialize storage service and ensure bucket exists"""
        try:
            async with self.session.client('s3') as s3:
                # Check if bucket exists
                try:
                    await s3.head_bucket(Bucket=self.bucket_name)
                    logger.info(f"S3 bucket {self.bucket_name} exists")
                except ClientError:
                    # Create bucket if it doesn't exist
                    logger.info(f"Creating S3 bucket {self.bucket_name}")
                    await s3.create_bucket(
                        Bucket=self.bucket_name,
                        CreateBucketConfiguration={'LocationConstraint': self.region}
                    )
                    
                    # Enable CORS
                    cors_config = {
                        'CORSRules': [{
                            'AllowedOrigins': ['*'],
                            'AllowedMethods': ['GET', 'PUT', 'POST'],
                            'AllowedHeaders': ['*'],
                            'MaxAgeSeconds': 3000
                        }]
                    }
                    await s3.put_bucket_cors(
                        Bucket=self.bucket_name,
                        CORSConfiguration=cors_config
                    )
                    logger.info(f"S3 bucket {self.bucket_name} created successfully")
        
        except Exception as e:
            logger.error(f"Failed to initialize S3: {e}")
            raise
    
    async def upload_file(
        self,
        file_data: bytes,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None
    ) -> str:
        """Upload file to S3"""
        try:
            async with self.session.client('s3') as s3:
                extra_args = {
                    'ContentType': content_type,
                    'ACL': 'public-read'
                }
                
                if metadata:
                    extra_args['Metadata'] = metadata
                
                # Upload file
                await s3.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=file_data,
                    **extra_args
                )
                
                # Return URL
                return self.get_url(key)
        
        except Exception as e:
            logger.error(f"Failed to upload file to S3: {e}")
            raise
    
    async def download_file(self, key: str) -> bytes:
        """Download file from S3"""
        try:
            async with self.session.client('s3') as s3:
                response = await s3.get_object(Bucket=self.bucket_name, Key=key)
                async with response['Body'] as stream:
                    return await stream.read()
        
        except Exception as e:
            logger.error(f"Failed to download file from S3: {e}")
            raise
    
    async def delete_file(self, key: str):
        """Delete file from S3"""
        try:
            async with self.session.client('s3') as s3:
                await s3.delete_object(Bucket=self.bucket_name, Key=key)
                logger.info(f"Deleted file from S3: {key}")
        
        except Exception as e:
            logger.error(f"Failed to delete file from S3: {e}")
            raise
    
    def get_url(self, key: str) -> str:
        """Get URL for S3 object"""
        if self.cloudfront_domain:
            return f"https://{self.cloudfront_domain}/{key}"
        else:
            return f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{key}"
    
    async def generate_presigned_url(
        self,
        key: str,
        expiration: int = 3600,
        http_method: str = "GET"
    ) -> str:
        """Generate presigned URL for temporary access"""
        try:
            async with self.session.client('s3') as s3:
                url = await s3.generate_presigned_url(
                    ClientMethod=f'{http_method.lower()}_object',
                    Params={
                        'Bucket': self.bucket_name,
                        'Key': key
                    },
                    ExpiresIn=expiration
                )
                return url
        
        except Exception as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            raise
    
    async def list_files(self, prefix: str = "") -> list:
        """List files in S3 with given prefix"""
        try:
            async with self.session.client('s3') as s3:
                response = await s3.list_objects_v2(
                    Bucket=self.bucket_name,
                    Prefix=prefix
                )
                
                return [obj['Key'] for obj in response.get('Contents', [])]
        
        except Exception as e:
            logger.error(f"Failed to list files from S3: {e}")
            raise
    
    async def cleanup(self):
        """Cleanup resources"""
        logger.info("Storage service cleanup complete")


# Global instance
storage_service = StorageService()
